#!/usr/bin/env python3
"""Camera frame grabber — separate process because agibot_gdk.Camera() deadlocks
when created in the same process as a GDK DDS Node (observed 2026-07-12).

Grabs multiple cameras each cycle and writes each latest JPEG atomically to
<outdir>/frame_<cam>.jpg (tmp + rename). GDK delivers frames already
JPEG-encoded so no transcoding happens here.
Exits by itself when its parent (bridge_daemon) dies.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, '/home/agi/app/gdk/lib')
sys.path.insert(0, '/home/agi/app/local/lib/python3.10/dist-packages')

import agibot_gdk  # noqa: E402


def cam_enum(name):
    return getattr(agibot_gdk.CameraType, "k" + "".join(w.capitalize() for w in name.split("_")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", default="head_color,hand_left_color,hand_right_color")
    ap.add_argument("--fps", type=float, default=4.0)
    ap.add_argument("--outdir", default="/dev/shm/robolabel_bridge")
    args = ap.parse_args()

    cams = [c for c in args.cams.split(",") if c]
    os.makedirs(args.outdir, exist_ok=True)
    ctypes = {c: cam_enum(c) for c in cams}
    interval = 1.0 / max(args.fps, 0.5)
    parent = os.getppid()

    cam = agibot_gdk.Camera()
    time.sleep(2.0)
    print(f"streamer ready ({cams})", flush=True)

    while True:
        if os.getppid() != parent:  # daemon died -> exit
            sys.exit(0)
        t0 = time.time()
        for name, ctype in ctypes.items():
            try:
                img = cam.get_latest_image(ctype, 500.0)
                if img is not None and img.encoding == agibot_gdk.Encoding.JPEG:
                    data = bytes(img.data)
                    if data:
                        out = os.path.join(args.outdir, f"frame_{name}.jpg")
                        with open(out + ".tmp", "wb") as f:
                            f.write(data)
                        os.replace(out + ".tmp", out)
            except Exception as e:
                print(f"streamer error ({name}): {e}", flush=True)
                time.sleep(0.5)
        dt = time.time() - t0
        if dt < interval:
            time.sleep(interval - dt)


if __name__ == "__main__":
    main()

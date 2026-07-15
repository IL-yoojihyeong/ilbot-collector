"""ffmpeg helpers (uses the static binary bundled with imageio-ffmpeg)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def encode_h265_frames(
    src_h265: Path,
    dst_mp4: Path,
    indices,
    fps: float,
    crf: int = 23,
) -> int:
    """Re-time a raw HEVC stream onto an aligned timeline and encode to
    browser-friendly H.264 mp4.

    `indices` is a monotonic non-decreasing array: output frame k is stream
    frame indices[k]. Repeated indices duplicate a frame (camera drop), skips
    discard frames. Implemented as a single streaming decode piped into the
    encoder, so memory use stays at one frame regardless of stream length.

    Returns the number of frames written.
    """
    import numpy as np

    indices = np.asarray(indices, dtype=np.int64)
    w, h = dimensions(src_h265, input_format="hevc")
    frame_size = w * h * 3 // 2  # yuv420p
    dst_mp4.parent.mkdir(parents=True, exist_ok=True)

    dec = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error",
         "-f", "hevc", "-r", str(fps), "-i", str(src_h265),
         "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
        stdout=subprocess.PIPE,
        # early termination after the last needed frame is expected; silence
        # the resulting broken-pipe complaints
        stderr=subprocess.DEVNULL)
    enc = subprocess.Popen(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-r", str(fps),
         "-i", "-",
         "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
         "-pix_fmt", "yuv420p",
         "-g", str(int(round(fps))),         # 1s GOP => fast seeking in the viewer
         "-movflags", "+faststart",
         str(dst_mp4)],
        stdin=subprocess.PIPE)

    pos = 0        # next output frame to satisfy
    written = 0
    last_needed = int(indices[-1])
    try:
        for j in range(last_needed + 1):
            buf = dec.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            while pos < len(indices) and indices[pos] == j:
                enc.stdin.write(buf)
                written += 1
                pos += 1
    finally:
        dec.stdout.close()
        dec.terminate()
        dec.wait()
        enc.stdin.close()
        enc.wait()

    if pos != len(indices):
        raise RuntimeError(
            f"{src_h265.name}: stream ended early, wrote {written}/{len(indices)} frames")
    return written


def probe(path: Path) -> dict:
    """Minimal stream info (width/height/nb_frames) using ffmpeg itself.

    imageio-ffmpeg ships no ffprobe, so decode headers via a null run.
    """
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path), "-map", "0:v:0", "-c", "copy",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    info: dict = {}
    for line in r.stderr.splitlines():
        line = line.strip()
        if line.startswith("Stream #") and "Video:" in line:
            for part in line.split(","):
                part = part.strip()
                token = part.split(" ")[0]
                if "x" in token:
                    wh = token.split("x")
                    if len(wh) == 2 and wh[0].isdigit() and wh[1].split()[0].isdigit():
                        info["width"], info["height"] = int(wh[0]), int(wh[1].split()[0])
        if line.startswith("frame="):
            info["nb_frames"] = int(line.split("=")[1].split()[0].split("fps")[0])
    return info


def count_frames(path: Path) -> int:
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path), "-map", "0:v:0",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    n = -1
    for line in r.stderr.splitlines():
        if line.strip().startswith("frame="):
            n = int(line.split("=")[1].split()[0])
    return n


def dimensions(path: Path, input_format: str | None = None) -> tuple[int, int]:
    """(width, height) of the first video stream."""
    cmd = [FFMPEG, "-hide_banner"]
    if input_format:
        cmd += ["-f", input_format]
    cmd += ["-i", str(path), "-frames:v", "0", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "Video:" in line:
            for part in line.split(","):
                token = part.strip().split(" ")[0]
                if "x" in token:
                    a, _, b = token.partition("x")
                    if a.isdigit() and b.isdigit():
                        return int(a), int(b)
    raise RuntimeError(f"could not determine dimensions of {path}")


def sample_frames(mp4: Path, n: int = 32) -> np.ndarray:
    """Decode ~n evenly spaced frames as (k, H, W, 3) uint8 for image stats."""
    total = count_frames(mp4)
    w, h = dimensions(mp4)
    step = max(1, total // n)
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(mp4),
         "-vf", f"select='not(mod(n\\,{step}))'", "-vsync", "vfr",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
    )
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    k = len(buf) // (h * w * 3)
    return buf[: k * h * w * 3].reshape(k, h, w, 3)

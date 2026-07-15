#!/usr/bin/env python3
"""Verify recorded teleop episode: rates, motion, camera/joint PTP sync."""
import os
import struct
import sys

sys.path.insert(0, '/home/agi/app/gdk/lib')
sys.path.insert(0, '/home/agi/app/local/lib/python3.10/dist-packages')

BASE = sys.argv[1] if len(sys.argv) > 1 else \
    "/data/record/3f2b8c41-9d7e-4b1a-a6c5-100000000005"


def read_pbdat(path):
    """Return (declared_total, [(ptp_ns, payload_bytes), ...])."""
    out = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        total = struct.unpack("q", f.read(8))[0]
        while f.tell() < size:
            ptp = struct.unpack("q", f.read(8))[0]
            f.seek(24, 1)
            ln = struct.unpack("q", f.read(8))[0]
            out.append((ptp, f.read(ln)))
    return total, out


print("== pbdat overview ==")
rec_dir = os.path.join(BASE, "record")
stamps = {}
for name in sorted(os.listdir(rec_dir)):
    if not name.endswith(".pbdat"):
        continue
    path = os.path.join(rec_dir, name)
    try:
        total, frames = read_pbdat(path)
    except Exception as e:
        print("%-40s parse error: %s" % (name, e))
        continue
    if frames:
        span = (frames[-1][0] - frames[0][0]) / 1e9
        rate = (len(frames) - 1) / span if span > 0 else 0
        print("%-40s n=%-6d span=%6.2fs rate=%6.1fHz" % (name, len(frames), span, rate))
        stamps[name] = [f[0] for f in frames]
    else:
        print("%-40s EMPTY" % name)

print()
print("== joint motion check (joint_state 22 dims) ==")
from genie_msgs_pb.msg.JointState_pb2 import JointState
_, js_frames = read_pbdat(os.path.join(rec_dir, "-hal-joint_state.pbdat"))
import array
n_dim = None
mins = maxs = None
for ptp, payload in js_frames:
    m = JointState()
    m.ParseFromString(payload)
    pos = list(m.position)
    if mins is None:
        n_dim = len(pos)
        mins = pos[:]
        maxs = pos[:]
    else:
        for i, v in enumerate(pos):
            if v < mins[i]:
                mins[i] = v
            if v > maxs[i]:
                maxs[i] = v
ranges = [maxs[i] - mins[i] for i in range(n_dim)]
moving = [i for i, r in enumerate(ranges) if r > 0.02]
print("dims=%d  max_range=%.4f rad  moving_joints(>0.02rad)=%s" % (n_dim, max(ranges), moving))

print()
print("== camera checks ==")
cam_ts = {}
for cam in sorted(os.listdir(os.path.join(BASE, "camera"))):
    txt = os.path.join(BASE, "camera", cam, cam + ".txt")
    ts = []
    with open(txt) as f:
        for line in f:
            parts = line.split()
            if parts:
                ts.append(int(parts[0]))
    cam_ts[cam] = ts
    if len(ts) > 1:
        span = (ts[-1] - ts[0]) / 1e9
        dts = [(ts[i + 1] - ts[i]) / 1e6 for i in range(len(ts) - 1)]
        gaps = sum(1 for d in dts if d > 50)
        print("%-18s frames=%-4d span=%6.2fs fps=%5.2f mean_dt=%5.1fms max_dt=%6.1fms gaps>50ms=%d"
              % (cam, len(ts), span, (len(ts) - 1) / span, sum(dts) / len(dts), max(dts), gaps))

print()
print("== camera <-> joint_state PTP sync ==")
js_ts = [f[0] for f in js_frames]
import bisect
for cam, ts in cam_ts.items():
    deltas = []
    for t in ts:
        i = bisect.bisect_left(js_ts, t)
        best = min((abs(js_ts[j] - t) for j in (i - 1, i, i + 1)
                    if 0 <= j < len(js_ts)))
        deltas.append(best / 1e6)
    print("%-18s nearest-joint |dt|: mean=%5.2fms max=%5.2fms" % (cam, sum(deltas) / len(deltas), max(deltas)))

print()
print("== VR data check ==")
vr = os.path.join(rec_dir, "-remote-vr_data.pbdat")
if os.path.exists(vr):
    _, vf = read_pbdat(vr)
    print("vr_data msgs:", len(vf))
    if vf:
        span = (vf[-1][0] - vf[0][0]) / 1e9
        print("vr_data rate: %.1fHz" % ((len(vf) - 1) / span if span > 0 else 0))

#!/usr/bin/env python3
"""Build record/aligned_joints.h5 for an Agibot G2 episode from pbdat + camera txt.

Produces the minimal schema that RoboLabel's G2 converter reads:
  <frame>/main_timestamp                    () uint64   reference (head cam) ts
  <frame>/state|action/joint/position       (14,) f64   arm_l 7 + arm_r 7  [rad]
  <frame>/state|action/left_effector/position  (1,) f64
  <frame>/state|action/right_effector/position (1,) f64
  <frame>/state|action/head/position        (3,) f64
  <frame>/state|action/waist/position       (5,) f64
  <frame>/timestamp/camera/<cam>            (1,) uint64  ts present in <cam>.txt

Joint sources (verified 2026-07-12 on G2, sw 2.2.0):
  state  <- /hal/joint_state  motor_position[22]  (position field = encoder counts!)
  action <- /hal/joint_cmd    position[22]        (already radians)
  layout: [0:5] waist(idx01-05), [5:8] head(idx11-13), [8:15] arm_l, [15:22] arm_r
  grippers <- /hal/{left,right}_ee_data position[0]; missing/broken -> 0.0

Usage: g2_h5_builder.py <episode_dir> [ref_cam] [out_path]
"""
import os
import struct
import sys
import bisect

sys.path.insert(0, '/home/agi/app/gdk/lib')
sys.path.insert(0, '/home/agi/app/local/lib/python3.10/dist-packages')

import numpy as np
import h5py
from genie_msgs_pb.msg.JointState_pb2 import JointState
from genie_msgs_pb.msg.EndState_pb2 import EndState
from genie_msgs_pb.msg.JointCommand_pb2 import JointCommand

WAIST = slice(0, 5)
HEAD = slice(5, 8)
ARMS = slice(8, 22)


def read_pbdat(path):
    """Yield (ptp_ns, payload) frames."""
    out = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.read(8)  # declared frame count
        while f.tell() < size:
            ptp = struct.unpack("q", f.read(8))[0]
            f.seek(24, 1)
            ln = struct.unpack("q", f.read(8))[0]
            out.append((ptp, f.read(ln)))
    return out


def parse_joint_series(path, msg_cls, field):
    ts, vecs, names = [], [], None
    for ptp, payload in read_pbdat(path):
        m = msg_cls()
        m.ParseFromString(payload)
        v = list(getattr(m, field))
        if len(v) < 22:
            continue
        ts.append(ptp)
        vecs.append(v[:22])
        if names is None and m.name:
            names = list(m.name)[:22]
    return np.array(ts, dtype=np.int64), np.array(vecs, dtype=np.float64), names or []


def parse_gripper_series(path):
    # ee_data 실제 타입은 EndState — end_state.position이 그리퍼 개도
    # (bridge/h5_builder._parse_gripper와 동일 로직 유지할 것)
    ts, vals = [], []
    for ptp, payload in read_pbdat(path):
        m = EndState()
        try:
            m.ParseFromString(payload)
        except Exception:
            continue
        if len(m.end_state):                        # repeated MotorState
            ts.append(ptp)
            vals.append(float(m.end_state[0].position))
    if not ts:                                      # legacy fallback
        for ptp, payload in read_pbdat(path):
            m = JointState()
            try:
                m.ParseFromString(payload)
            except Exception:
                continue
            pos = list(m.position)
            if pos:
                ts.append(ptp)
                vals.append(pos[0])
    return np.array(ts, dtype=np.int64), np.array(vals, dtype=np.float64)


def nearest_idx(sorted_ts, t):
    i = bisect.bisect_left(sorted_ts, t)
    best, best_d = None, None
    for j in (i - 1, i):
        if 0 <= j < len(sorted_ts):
            d = abs(int(sorted_ts[j]) - int(t))
            if best is None or d < best_d:
                best, best_d = j, d
    return best


def main():
    ep = sys.argv[1]
    ref_cam = sys.argv[2] if len(sys.argv) > 2 else "head_color"
    out_path = sys.argv[3] if len(sys.argv) > 3 else os.path.join(ep, "record", "aligned_joints.h5")
    rec = os.path.join(ep, "record")

    cams = sorted(d for d in os.listdir(os.path.join(ep, "camera"))
                  if os.path.isfile(os.path.join(ep, "camera", d, d + ".txt")))
    cam_ts = {}
    for c in cams:
        with open(os.path.join(ep, "camera", c, c + ".txt")) as f:
            cam_ts[c] = np.array([int(l.split()[0]) for l in f if l.strip()], dtype=np.int64)
    if ref_cam not in cam_ts:
        sys.exit("ref cam %s missing" % ref_cam)

    js_ts, js_vec, js_names = parse_joint_series(
        os.path.join(rec, "-hal-joint_state.pbdat"), JointState, "motor_position")
    jc_ts, jc_vec, jc_names = parse_joint_series(
        os.path.join(rec, "-hal-joint_cmd.pbdat"), JointCommand, "position")
    names = js_names or jc_names

    grip = {}
    for side in ("left", "right"):
        p = os.path.join(rec, "-hal-%s_ee_data.pbdat" % side)
        if os.path.exists(p):
            grip[side] = parse_gripper_series(p)
        else:
            grip[side] = (np.array([], dtype=np.int64), np.array([]))
        if len(grip[side][0]) == 0:
            print("WARN: %s gripper has no position data -> 0.0" % side)

    t0 = max([int(v[0]) for v in cam_ts.values()] + [int(js_ts[0]), int(jc_ts[0])])
    t1 = min([int(v[-1]) for v in cam_ts.values()] + [int(js_ts[-1]), int(jc_ts[-1])])
    frames = [int(t) for t in cam_ts[ref_cam] if t0 <= t <= t1]
    if not frames:
        sys.exit("no overlapping frames")
    print("cams=%s ref=%s frames=%d span=%.2fs (trimmed from %d ref frames)"
          % (cams, ref_cam, len(frames), (frames[-1] - frames[0]) / 1e9, len(cam_ts[ref_cam])))

    grp_defs = [("joint", ARMS, 14), ("head", HEAD, 3), ("waist", WAIST, 5)]
    attr_names = {
        "joint": names[ARMS] if names else [],
        "head": names[HEAD] if names else [],
        "waist": names[WAIST] if names else [],
        "left_effector": ["idx31_gripper_l_inner_joint1"],
        "right_effector": ["idx71_gripper_r_inner_joint1"],
    }

    with h5py.File(out_path, "w") as h:
        for i, ts in enumerate(frames):
            g = h.create_group(str(i))
            g.create_dataset("main_timestamp", data=np.uint64(ts))
            sj = js_vec[nearest_idx(js_ts, ts)]
            aj = jc_vec[nearest_idx(jc_ts, ts)]
            for side_key, vec in (("state", sj), ("action", aj)):
                sg = g.create_group(side_key)
                for gname, sl, dim in grp_defs:
                    gg = sg.create_group(gname)
                    gg.create_dataset("position", data=np.asarray(vec[sl], dtype=np.float64))
                for side, gname in (("left", "left_effector"), ("right", "right_effector")):
                    gts, gvals = grip[side]
                    val = float(gvals[nearest_idx(gts, ts)]) if len(gts) else 0.0
                    sg.create_group(gname).create_dataset(
                        "position", data=np.array([val], dtype=np.float64))
                if side_key == "state":
                    for gname, nm in attr_names.items():
                        if len(nm):
                            sg[gname].attrs["name"] = [str(x) for x in nm]
            tg = g.create_group("timestamp/camera")
            for c in cams:
                j = nearest_idx(cam_ts[c], ts)
                tg.create_dataset(c, data=np.array([cam_ts[c][j]], dtype=np.uint64))
    print("wrote %s (%d frames)" % (out_path, len(frames)))


if __name__ == "__main__":
    main()

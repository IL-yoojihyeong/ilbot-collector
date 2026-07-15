"""Build record/aligned_joints.h5 from pbdat + camera txt — laptop version.

Same logic as robot/g2_h5_builder.py (validated end-to-end 2026-07-12) but
importable. Protos default to the vendored set (GDK 2.6.3 기준); a different
GDK's protos can be selected with env ROBOLABEL_PROTO_DIR — in that case use
build_auto(proto_dir=...) which runs this module as a subprocess so the two
proto sets never collide in one descriptor pool.
"""
import bisect
import json
import os
import struct
import subprocess
import sys

import h5py
import numpy as np

from .config import REPO_ROOT, VENDOR_DIR

PROTO_DIR = os.environ.get("ROBOLABEL_PROTO_DIR") or str(VENDOR_DIR)
sys.path.insert(0, PROTO_DIR)
from genie_msgs_pb.msg.JointState_pb2 import JointState        # noqa: E402
from genie_msgs_pb.msg.JointCommand_pb2 import JointCommand    # noqa: E402
from genie_msgs_pb.msg.EndState_pb2 import EndState            # noqa: E402

WAIST = slice(0, 5)
HEAD = slice(5, 8)
ARMS = slice(8, 22)


def read_pbdat(path):
    out = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.read(8)
        while f.tell() < size:
            ptp = struct.unpack("q", f.read(8))[0]
            f.seek(24, 1)
            ln = struct.unpack("q", f.read(8))[0]
            out.append((ptp, f.read(ln)))
    return out


def _parse_joint_series(path, msg_cls, fld):
    ts, vecs, names = [], [], None
    for ptp, payload in read_pbdat(path):
        m = msg_cls()
        m.ParseFromString(payload)
        v = list(getattr(m, fld))
        if len(v) < 22:
            continue
        ts.append(ptp)
        vecs.append(v[:22])
        if names is None and m.name:
            names = list(m.name)[:22]
    return np.array(ts, dtype=np.int64), np.array(vecs, dtype=np.float64), names or []


def _parse_gripper(path):
    """/hal/*_ee_data의 실제 타입은 EndState (2026-07-14 aorta 레지스트리로 확인) —
    그리퍼 개도는 end_state.position. 과거엔 JointState로 오파싱해 항상 빈 값이
    나왔다(이전 로봇의 '그리퍼 고장' 판정도 이 오해였을 가능성). 혹시 다른
    버전이 JointState로 발행하면 fallback으로 처리."""
    ts, vals = [], []
    if os.path.exists(path):
        for ptp, payload in read_pbdat(path):
            m = EndState()
            try:
                m.ParseFromString(payload)
            except Exception:
                continue
            if len(m.end_state):                    # repeated MotorState
                ts.append(ptp)
                vals.append(float(m.end_state[0].position))
        if not ts:                                  # legacy fallback
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


def _nearest(sorted_ts, t):
    i = bisect.bisect_left(sorted_ts, t)
    best, best_d = None, None
    for j in (i - 1, i):
        if 0 <= j < len(sorted_ts):
            d = abs(int(sorted_ts[j]) - int(t))
            if best is None or d < best_d:
                best, best_d = j, d
    return best


def build(episode_dir, ref_cam="head_color", out_path=None, log=print):
    """Build aligned_joints.h5. Returns (out_path, n_frames, warnings)."""
    ep = str(episode_dir)
    rec_dir = os.path.join(ep, "record")
    out_path = out_path or os.path.join(rec_dir, "aligned_joints.h5")
    warnings = []

    cams = sorted(d for d in os.listdir(os.path.join(ep, "camera"))
                  if os.path.isfile(os.path.join(ep, "camera", d, d + ".txt")))
    cam_ts = {}
    for c in cams:
        with open(os.path.join(ep, "camera", c, c + ".txt")) as f:
            cam_ts[c] = np.array([int(l.split()[0]) for l in f if l.strip()], dtype=np.int64)
    if ref_cam not in cam_ts:
        raise ValueError(f"reference camera {ref_cam} not found (have {cams})")

    js_ts, js_vec, js_names = _parse_joint_series(
        os.path.join(rec_dir, "-hal-joint_state.pbdat"), JointState, "motor_position")
    jc_ts, jc_vec, jc_names = _parse_joint_series(
        os.path.join(rec_dir, "-hal-joint_cmd.pbdat"), JointCommand, "position")
    names = js_names or jc_names
    if len(js_ts) == 0 or len(jc_ts) == 0:
        raise ValueError("joint_state / joint_cmd pbdat empty")

    grip = {}
    for side in ("left", "right"):
        grip[side] = _parse_gripper(os.path.join(rec_dir, f"-hal-{side}_ee_data.pbdat"))
        if len(grip[side][0]) == 0:
            warnings.append(f"{side} gripper has no position data -> 0.0")

    t0 = max([int(v[0]) for v in cam_ts.values()] + [int(js_ts[0]), int(jc_ts[0])])
    t1 = min([int(v[-1]) for v in cam_ts.values()] + [int(js_ts[-1]), int(jc_ts[-1])])
    frames = [int(t) for t in cam_ts[ref_cam] if t0 <= t <= t1]
    if not frames:
        raise ValueError("no overlapping frames across streams")
    log(f"h5: {len(frames)} frames, span {(frames[-1]-frames[0])/1e9:.2f}s, cams={cams}")

    grp_defs = [("joint", ARMS), ("head", HEAD), ("waist", WAIST)]
    attr_names = {
        "joint": names[ARMS] if names else [],
        "head": names[HEAD] if names else [],
        "waist": names[WAIST] if names else [],
        "left_effector": ["idx31_gripper_l_inner_joint1"],
        "right_effector": ["idx71_gripper_r_inner_joint1"],
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with h5py.File(out_path, "w") as h:
        for i, ts in enumerate(frames):
            g = h.create_group(str(i))
            g.create_dataset("main_timestamp", data=np.uint64(ts))
            sj = js_vec[_nearest(js_ts, ts)]
            aj = jc_vec[_nearest(jc_ts, ts)]
            for side_key, vec in (("state", sj), ("action", aj)):
                sg = g.create_group(side_key)
                for gname, sl in grp_defs:
                    sg.create_group(gname).create_dataset(
                        "position", data=np.asarray(vec[sl], dtype=np.float64))
                for side, gname in (("left", "left_effector"), ("right", "right_effector")):
                    gts, gvals = grip[side]
                    val = float(gvals[_nearest(gts, ts)]) if len(gts) else 0.0
                    sg.create_group(gname).create_dataset(
                        "position", data=np.array([val], dtype=np.float64))
                if side_key == "state":
                    for gname, nm in attr_names.items():
                        if len(nm):
                            sg[gname].attrs["name"] = [str(x) for x in nm]
            tg = g.create_group("timestamp/camera")
            for c in cams:
                j = _nearest(cam_ts[c], ts)
                tg.create_dataset(c, data=np.array([cam_ts[c][j]], dtype=np.uint64))
    return out_path, len(frames), warnings


def build_auto(episode_dir, ref_cam="head_color", proto_dir=None,
               out_path=None, log=print):
    """proto_dir가 없으면(=vendored 호환) 인프로세스 build, 있으면 그 proto로
    서브프로세스 빌드 (로봇 프로파일의 proto_dir — 비호환 GDK 대응)."""
    if not proto_dir or str(proto_dir) == str(VENDOR_DIR):
        return build(episode_dir, ref_cam, out_path=out_path, log=log)
    cmd = [sys.executable, "-m", "bridge.h5_builder", str(episode_dir), ref_cam]
    if out_path:
        cmd.append(str(out_path))
    env = dict(os.environ, ROBOLABEL_PROTO_DIR=str(proto_dir),
               PYTHONPATH=str(REPO_ROOT))
    log(f"h5: 로봇 proto로 빌드 ({proto_dir})")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    result = None
    for line in r.stdout.splitlines():
        if line.startswith("RESULT "):
            result = json.loads(line[7:])
        else:
            log(line)
    if r.returncode != 0 or result is None:
        raise RuntimeError(f"h5 subprocess 실패: {(r.stderr or r.stdout)[-400:]}")
    return result["out_path"], result["n_frames"], result["warnings"]


if __name__ == "__main__":
    ep, ref = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "head_color"
    out = sys.argv[3] if len(sys.argv) > 3 else None
    o, n, w = build(ep, ref, out_path=out, log=lambda m: print(m, flush=True))
    print("RESULT " + json.dumps({"out_path": o, "n_frames": n, "warnings": w}))

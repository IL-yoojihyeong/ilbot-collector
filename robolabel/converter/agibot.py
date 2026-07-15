"""Reader for Agibot G2 episode directories.

An episode directory looks like:

    <episode>/
      meta_info.json
      camera/<cam>/<cam>.h265      raw HEVC elementary stream
      camera/<cam>/<cam>.txt       one line per frame: "<ns_timestamp> <I|P>"
      record/aligned_joints.h5     per-frame groups "0".."N-1" with
                                   state/*, action/*, main_timestamp and
                                   timestamp/camera/<cam> alignment info
      parameters/...               intrinsics/extrinsics (kept as-is, not converted)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import h5py
import numpy as np

# state/action vector layout: (h5 subgroup, dataset, dim)
# Both state and action expose the same named layout so that
# observation.state[i] and action[i] refer to the same joint.
JOINT_LAYOUT = [
    ("joint", "position", 14),          # arm_l 7 + arm_r 7
    ("left_effector", "position", 1),   # gripper_l
    ("right_effector", "position", 1),  # gripper_r
    ("head", "position", 3),
    ("waist", "position", 5),
]
STATE_DIM = sum(d for _, _, d in JOINT_LAYOUT)


def _strip_idx(name: str) -> str:
    # 'idx21_arm_l_joint1' -> 'arm_l_joint1'
    return name.split("_", 1)[1] if name.startswith("idx") else name


@dataclass
class G2Episode:
    path: Path

    meta: dict = field(init=False)
    n_frames: int = field(init=False)
    state: np.ndarray = field(init=False)        # (T, STATE_DIM) float32
    action: np.ndarray = field(init=False)       # (T, STATE_DIM) float32
    joint_names: list[str] = field(init=False)   # len STATE_DIM
    timestamps_ns: np.ndarray = field(init=False)  # (T,) uint64 main_timestamp
    # camera name -> (T,) int64 timestamps of the camera frame aligned to each h5 frame
    camera_ts: dict[str, np.ndarray] = field(init=False)

    def __post_init__(self):
        self.path = Path(self.path)
        meta_path = self.path / "meta_info.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"not an Agibot G2 episode (no meta_info.json): {self.path}")
        self.meta = json.loads(meta_path.read_text())
        self._load_h5()

    # ---------------------------------------------------------------- h5
    def _load_h5(self):
        h5_path = self.path / "record" / "aligned_joints.h5"
        with h5py.File(h5_path, "r") as f:
            frame_keys = sorted((k for k in f.keys() if k.isdigit()), key=int)
            T = len(frame_keys)
            if T == 0:
                raise ValueError(f"no frames in {h5_path}")
            if int(frame_keys[-1]) != T - 1:
                raise ValueError(f"non-contiguous frame indices in {h5_path}")

            self.n_frames = T
            self.state = np.empty((T, STATE_DIM), dtype=np.float32)
            self.action = np.empty((T, STATE_DIM), dtype=np.float32)
            self.timestamps_ns = np.empty(T, dtype=np.uint64)

            cam_names = list(f["0/timestamp/camera"].keys())
            self.camera_ts = {c: np.empty(T, dtype=np.int64) for c in cam_names}

            # joint names from group attrs of frame 0
            names: list[str] = []
            for grp, _, dim in JOINT_LAYOUT:
                attr = f[f"0/state/{grp}"].attrs.get("name")
                if attr is not None and len(attr) == dim:
                    names += [_strip_idx(str(n)) for n in np.asarray(attr).tolist()]
                else:
                    names += [f"{grp}_{i}" for i in range(dim)]
            self.joint_names = names

            for i, k in enumerate(frame_keys):
                g = f[k]
                self.timestamps_ns[i] = g["main_timestamp"][()]
                off = 0
                for grp, ds, dim in JOINT_LAYOUT:
                    self.state[i, off:off + dim] = g[f"state/{grp}/{ds}"][()]
                    self.action[i, off:off + dim] = g[f"action/{grp}/{ds}"][()]
                    off += dim
                for c in cam_names:
                    self.camera_ts[c][i] = g[f"timestamp/camera/{c}"][()][0]

    # ------------------------------------------------------------- video
    def camera_stream(self, cam: str) -> Path:
        p = self.path / "camera" / cam / f"{cam}.h265"
        if not p.exists():
            raise FileNotFoundError(p)
        return p

    def camera_frame_timestamps(self, cam: str) -> np.ndarray:
        """All frame timestamps (ns) of the raw camera stream, in stream order."""
        txt = self.path / "camera" / cam / f"{cam}.txt"
        return np.array([int(line.split()[0]) for line in txt.read_text().splitlines() if line.strip()],
                        dtype=np.int64)

    def aligned_frame_indices(self, cam: str) -> np.ndarray:
        """Stream frame index chosen for each h5 frame, shape (T,).

        The h5 file stores, for every aligned frame, the timestamp of the camera
        frame chosen for it. The result is monotonic non-decreasing: an index may
        repeat (camera dropped a frame, nearest one reused) or skip ahead.
        """
        stream_ts = self.camera_frame_timestamps(cam)
        index_of = {int(t): i for i, t in enumerate(stream_ts)}
        want = self.camera_ts[cam]
        try:
            idx = np.array([index_of[int(t)] for t in want], dtype=np.int64)
        except KeyError as e:
            raise ValueError(f"{cam}: h5 references timestamp {e} not present in stream") from None
        if np.any(np.diff(idx) < 0):
            raise ValueError(f"{cam}: aligned frame indices are not monotonic")
        return idx

    @property
    def fps(self) -> float:
        if self.n_frames < 2:
            return 30.0
        dur = float(self.timestamps_ns[-1] - self.timestamps_ns[0]) / 1e9
        return round((self.n_frames - 1) / dur, 2)

    @property
    def available_cameras(self) -> list[str]:
        """Cameras that have both an alignment track in the h5 and an h265 stream."""
        out = []
        for c in self.camera_ts:
            if (self.path / "camera" / c / f"{c}.h265").exists():
                out.append(c)
        return sorted(out)

"""LeRobot v3.0 dataset writer.

Layout produced (one data file and one video file per episode; chunks roll
every `chunks_size` files):

    meta/info.json
    meta/tasks.parquet
    meta/stats.json
    meta/episodes/chunk-000/file-000.parquet
    data/chunk-000/file-<episode>.parquet
    videos/<video_key>/chunk-000/file-<episode>.mp4
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

CODEBASE_VERSION = "v3.0"
CHUNKS_SIZE = 1000


def _chunk_file(index: int) -> tuple[int, int]:
    return index // CHUNKS_SIZE, index % CHUNKS_SIZE


def _num_stats(arr: np.ndarray) -> dict:
    a = arr.astype(np.float64).reshape(len(arr), -1)
    return {
        "min": a.min(0).tolist(),
        "max": a.max(0).tolist(),
        "mean": a.mean(0).tolist(),
        "std": a.std(0).tolist(),
        "count": [len(a)],
    }


def _img_stats(frames: np.ndarray) -> dict:
    # frames: (k, H, W, 3) uint8 -> per-channel stats in [0,1], shape (3,1,1)
    x = frames.astype(np.float64) / 255.0
    flat = x.reshape(-1, x.shape[-1])  # (k*H*W, 3)
    def shape3(v):
        return [[[float(c)]] for c in v]
    return {
        "min": shape3(flat.min(0)),
        "max": shape3(flat.max(0)),
        "mean": shape3(flat.mean(0)),
        "std": shape3(flat.std(0)),
        "count": [int(frames.shape[0])],
    }


def aggregate_stats(stats_list: list[dict]) -> dict:
    """Merge per-episode stats dicts into global dataset stats (meta/stats.json)."""
    agg: dict = {}
    for key in stats_list[0]:
        mins = np.array([s[key]["min"] for s in stats_list], dtype=np.float64)
        maxs = np.array([s[key]["max"] for s in stats_list], dtype=np.float64)
        means = np.array([s[key]["mean"] for s in stats_list], dtype=np.float64)
        stds = np.array([s[key]["std"] for s in stats_list], dtype=np.float64)
        counts = np.array([s[key]["count"][0] for s in stats_list], dtype=np.float64)
        w = counts / counts.sum()
        wshape = [-1] + [1] * (means.ndim - 1)
        wb = w.reshape(wshape)
        mean = (means * wb).sum(0)
        # var = E[x^2] - mean^2, with E[x^2] per episode = std^2 + mean^2
        ex2 = ((stds ** 2 + means ** 2) * wb).sum(0)
        std = np.sqrt(np.maximum(ex2 - mean ** 2, 0))
        agg[key] = {
            "min": mins.min(0).tolist(),
            "max": maxs.max(0).tolist(),
            "mean": mean.tolist(),
            "std": std.tolist(),
            "count": [int(counts.sum())],
        }
    return agg


@dataclass
class EpisodeData:
    """Everything the writer needs for one episode."""
    state: np.ndarray                     # (T, D) float32
    action: np.ndarray                    # (T, D) float32
    task: str                             # instruction text ("" if unlabeled yet)
    videos: dict[str, Path]               # video_key -> already-encoded mp4 (T frames)
    video_stats: dict[str, dict] = field(default_factory=dict)
    source_path: str = ""                 # provenance (original episode dir)


class LeRobotWriter:
    def __init__(self, root: Path, fps: float, robot_type: str,
                 joint_names: list[str], video_info: dict[str, dict]):
        """video_info: video_key -> {"width": int, "height": int}"""
        self.root = Path(root)
        self.fps = fps
        self.robot_type = robot_type
        self.joint_names = joint_names
        self.video_info = video_info
        self.episodes: list[dict] = []
        self.tasks: list[str] = []
        self._frame_offset = 0
        self._stats_acc: list[dict] = []
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"output dir not empty: {self.root}")

    @classmethod
    def resume(cls, root: Path) -> "LeRobotWriter":
        """기존 데이터셋을 열어 에피소드를 이어 붙일 수 있는 writer 복원.

        meta(info/tasks/episodes parquet의 stats 컬럼 포함)에서 내부 상태를
        재구성한다 — add_episode → finalize가 처음부터 쓴 것과 동일하게 동작.
        (finalize는 meta를 통째로 재작성하므로 매 에피소드 후 호출해도 안전)
        """
        root = Path(root)
        info = json.loads((root / "meta/info.json").read_text())
        feats = info["features"]
        names = feats["observation.state"].get("names") or []
        joint_names = names[0] if names and isinstance(names[0], list) else names
        video_info = {k: {"width": f["info"]["video.width"],
                          "height": f["info"]["video.height"]}
                      for k, f in feats.items() if f.get("dtype") == "video"}

        w = cls.__new__(cls)
        w.root = root
        w.fps = float(info.get("fps", 30))
        w.robot_type = str(info.get("robot_type", ""))
        w.joint_names = list(joint_names)
        w.video_info = video_info
        w.tasks = list(pq.read_table(root / "meta/tasks.parquet")
                       .to_pydict()["task"])
        w.episodes, w._stats_acc = [], []
        for f in sorted((root / "meta/episodes").rglob("*.parquet")):
            t = pq.read_table(f)
            stat_keys = sorted({c.split("/")[1] for c in t.column_names
                                if c.startswith("stats/")})
            for r in sorted(t.to_pylist(), key=lambda x: x["episode_index"]):
                stats = {k: {f2: r[f"stats/{k}/{f2}"]
                             for f2 in ("min", "max", "mean", "std", "count")}
                         for k in stat_keys}
                videos = {vk: {f2: r[f"videos/{vk}/{f2}"]
                               for f2 in ("chunk_index", "file_index",
                                          "from_timestamp", "to_timestamp")}
                          for vk in video_info}
                tasks_col = r.get("tasks") or [""]
                w.episodes.append({
                    "episode_index": r["episode_index"],
                    "chunk": r["data/chunk_index"], "file": r["data/file_index"],
                    "from_index": r["dataset_from_index"],
                    "to_index": r["dataset_to_index"],
                    "length": r["length"], "task": tasks_col[0],
                    "videos": videos, "stats": stats,
                    "source_path": r.get("source_path", ""),
                })
                w._stats_acc.append(stats)
        w._frame_offset = w.episodes[-1]["to_index"] if w.episodes else 0
        return w

    # ------------------------------------------------------------------ add
    def _task_index(self, task: str) -> int:
        if task not in self.tasks:
            self.tasks.append(task)
        return self.tasks.index(task)

    def add_episode(self, ep: EpisodeData) -> int:
        ep_index = len(self.episodes)
        T = len(ep.state)
        chunk, file = _chunk_file(ep_index)
        task_index = self._task_index(ep.task)

        # ---- data parquet
        ts = (np.arange(T) / self.fps).astype(np.float32)
        table = pa.table({
            "observation.state": pa.array(list(ep.state), type=pa.list_(pa.float32())),
            "action": pa.array(list(ep.action), type=pa.list_(pa.float32())),
            "timestamp": pa.array(ts, type=pa.float32()),
            "frame_index": pa.array(np.arange(T, dtype=np.int64)),
            "episode_index": pa.array(np.full(T, ep_index, dtype=np.int64)),
            "index": pa.array(np.arange(self._frame_offset, self._frame_offset + T, dtype=np.int64)),
            "task_index": pa.array(np.full(T, task_index, dtype=np.int64)),
        })
        data_path = self.root / f"data/chunk-{chunk:03d}/file-{file:03d}.parquet"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, data_path)

        # ---- move videos into place
        video_meta = {}
        for key, src in ep.videos.items():
            dst = self.root / f"videos/{key}/chunk-{chunk:03d}/file-{file:03d}.mp4"
            dst.parent.mkdir(parents=True, exist_ok=True)
            Path(src).replace(dst)
            video_meta[key] = {
                "chunk_index": chunk, "file_index": file,
                "from_timestamp": 0.0, "to_timestamp": round(T / self.fps, 6),
            }

        # ---- per-episode stats (numeric here; image stats passed in)
        stats = {
            "observation.state": _num_stats(ep.state),
            "action": _num_stats(ep.action),
            "timestamp": _num_stats(ts.reshape(-1, 1)),
            "frame_index": _num_stats(np.arange(T).reshape(-1, 1)),
            "episode_index": _num_stats(np.full((T, 1), ep_index)),
            "index": _num_stats(np.arange(self._frame_offset, self._frame_offset + T).reshape(-1, 1)),
            "task_index": _num_stats(np.full((T, 1), task_index)),
        }
        stats.update(ep.video_stats)
        self._stats_acc.append(stats)

        self.episodes.append({
            "episode_index": ep_index,
            "chunk": chunk, "file": file,
            "from_index": self._frame_offset,
            "to_index": self._frame_offset + T,
            "length": T,
            "task": ep.task,
            "videos": video_meta,
            "stats": stats,
            "source_path": ep.source_path,
        })
        self._frame_offset += T
        return ep_index

    # ------------------------------------------------------------- finalize
    def finalize(self, extra_info: dict | None = None):
        self._write_episodes_meta()
        self._write_tasks()
        self._write_info(extra_info or {})
        self._write_stats()

    def _write_episodes_meta(self):
        cols: dict[str, list] = {
            "episode_index": [], "data/chunk_index": [], "data/file_index": [],
            "dataset_from_index": [], "dataset_to_index": [],
        }
        for key in self.video_info:
            for f in ("chunk_index", "file_index", "from_timestamp", "to_timestamp"):
                cols[f"videos/{key}/{f}"] = []
        cols.update({"tasks": [], "length": [], "source_path": []})
        stat_keys = list(self._stats_acc[0].keys()) if self._stats_acc else []
        for sk in stat_keys:
            for f in ("min", "max", "mean", "std", "count"):
                cols[f"stats/{sk}/{f}"] = []
        cols["meta/episodes/chunk_index"] = []
        cols["meta/episodes/file_index"] = []

        for ep in self.episodes:
            cols["episode_index"].append(ep["episode_index"])
            cols["data/chunk_index"].append(ep["chunk"])
            cols["data/file_index"].append(ep["file"])
            cols["dataset_from_index"].append(ep["from_index"])
            cols["dataset_to_index"].append(ep["to_index"])
            for key in self.video_info:
                vm = ep["videos"][key]
                for f in ("chunk_index", "file_index", "from_timestamp", "to_timestamp"):
                    cols[f"videos/{key}/{f}"].append(vm[f])
            cols["tasks"].append([ep["task"]])
            cols["length"].append(ep["length"])
            cols["source_path"].append(ep["source_path"])
            for sk in stat_keys:
                for f in ("min", "max", "mean", "std", "count"):
                    cols[f"stats/{sk}/{f}"].append(ep["stats"][sk][f])
            cols["meta/episodes/chunk_index"].append(0)
            cols["meta/episodes/file_index"].append(0)

        path = self.root / "meta/episodes/chunk-000/file-000.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(cols), path)

    def _write_tasks(self):
        path = self.root / "meta/tasks.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({
            "task_index": pa.array(range(len(self.tasks)), type=pa.int64()),
            "task": pa.array(self.tasks, type=pa.string()),
        }), path)

    def _features(self) -> dict:
        d = len(self.joint_names)
        feats = {
            "observation.state": {"dtype": "float32", "shape": [d],
                                  "names": [self.joint_names], "fps": self.fps},
            "action": {"dtype": "float32", "shape": [d],
                       "names": [self.joint_names], "fps": self.fps},
        }
        for key, vi in self.video_info.items():
            feats[key] = {
                "dtype": "video",
                "shape": [3, vi["height"], vi["width"]],
                "names": ["channels", "height", "width"],
                "info": {
                    "video.height": vi["height"], "video.width": vi["width"],
                    "video.codec": "h264", "video.pix_fmt": "yuv420p",
                    "video.is_depth_map": False, "video.fps": self.fps,
                    "video.channels": 3, "has_audio": False,
                },
            }
        for key, dtype in (("timestamp", "float32"), ("frame_index", "int64"),
                           ("episode_index", "int64"), ("index", "int64"),
                           ("task_index", "int64")):
            feats[key] = {"dtype": dtype, "shape": [1], "names": None, "fps": self.fps}
        return feats

    def _write_info(self, extra: dict):
        info = {
            "codebase_version": CODEBASE_VERSION,
            "robot_type": self.robot_type,
            "total_episodes": len(self.episodes),
            "total_frames": self._frame_offset,
            "total_tasks": len(self.tasks),
            "chunks_size": CHUNKS_SIZE,
            "fps": self.fps,
            "splits": {"train": f"0:{len(self.episodes)}"},
            "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
            "features": self._features(),
            "data_files_size_in_mb": 100,
            "video_files_size_in_mb": 200,
        }
        info.update(extra)
        (self.root / "meta").mkdir(parents=True, exist_ok=True)
        (self.root / "meta/info.json").write_text(json.dumps(info, indent=4))

    def _write_stats(self):
        """Aggregate per-episode stats into global meta/stats.json."""
        if not self._stats_acc:
            return
        agg = aggregate_stats(self._stats_acc)
        (self.root / "meta/stats.json").write_text(json.dumps(agg, indent=4))

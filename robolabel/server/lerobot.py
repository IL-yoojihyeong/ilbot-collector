"""Read/annotate LeRobot v3.0 datasets on disk."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


def load_info(root: Path) -> dict:
    return json.loads((Path(root) / "meta/info.json").read_text())


def episodes_meta_files(root: Path) -> list[Path]:
    return sorted((Path(root) / "meta/episodes").rglob("*.parquet"))


def scan_episodes(root: Path) -> list[dict]:
    """One dict per episode: index, length, task, data file, per-video window."""
    root = Path(root)
    info = load_info(root)
    video_keys = [k for k, f in info["features"].items() if f["dtype"] == "video"]
    out = []
    for f in episodes_meta_files(root):
        t = pq.read_table(f)
        rows = t.to_pylist()
        for r in rows:
            videos = {}
            for vk in video_keys:
                rel = info["video_path"].format(
                    video_key=vk,
                    chunk_index=r[f"videos/{vk}/chunk_index"],
                    file_index=r[f"videos/{vk}/file_index"])
                videos[vk] = {
                    "rel_path": rel,
                    "from_ts": r.get(f"videos/{vk}/from_timestamp", 0.0),
                    "to_ts": r.get(f"videos/{vk}/to_timestamp", 0.0),
                }
            data_rel = info["data_path"].format(
                chunk_index=r["data/chunk_index"], file_index=r["data/file_index"])
            tasks = r.get("tasks") or []
            out.append({
                "episode_index": r["episode_index"],
                "length": r["length"],
                "task": tasks[0] if tasks else "",
                "data_file": data_rel,
                "videos": videos,
                "source_path": r.get("source_path", ""),
            })
    return out


def read_timeseries(root: Path, data_file: str, episode_index: int,
                    max_points: int = 1200) -> dict:
    """state/action arrays for one episode, downsampled for charting."""
    t = pq.read_table(Path(root) / data_file,
                      columns=["observation.state", "action", "frame_index",
                               "episode_index"])
    t = t.filter(pc.equal(t["episode_index"], episode_index))
    state = np.array(t["observation.state"].to_pylist(), dtype=np.float32)
    action = np.array(t["action"].to_pylist(), dtype=np.float32)
    frames = np.array(t["frame_index"].to_pylist(), dtype=np.int64)
    step = max(1, len(frames) // max_points)
    info = load_info(root)
    names = info["features"]["observation.state"].get("names")
    names = names[0] if names and isinstance(names[0], list) else names
    return {
        "frames": frames[::step].tolist(),
        "state": state[::step].round(5).tolist(),
        "action": action[::step].round(5).tolist(),
        "names": names or [f"dim_{i}" for i in range(state.shape[1])],
        "length": int(len(frames)),
    }


# ------------------------------------------------------------------ export
def export_labels(root: Path, episodes: list[dict]):
    """Write labels back into the dataset's meta files.

    episodes: [{episode_index, task_text, pass_status,
                segments: [{start_frame, end_frame, text, skill}]}]

    - meta/tasks.parquet is rebuilt from all episode task texts
    - every meta/episodes parquet gets updated 'tasks' plus 'action_config'
      (list<struct{start_frame,end_frame,action_text,skill}>) and
      'quality' (pass | non_pass | unlabeled) columns
    """
    root = Path(root)
    by_index = {e["episode_index"]: e for e in episodes}

    # rebuild task list
    tasks: list[str] = []
    for e in episodes:
        txt = e.get("task_text") or ""
        if txt not in tasks:
            tasks.append(txt)
    pq.write_table(pa.table({
        "task_index": pa.array(range(len(tasks)), type=pa.int64()),
        "task": pa.array(tasks, type=pa.string()),
    }), root / "meta/tasks.parquet")

    seg_type = pa.list_(pa.struct([
        ("start_frame", pa.int64()), ("end_frame", pa.int64()),
        ("action_text", pa.string()), ("skill", pa.string())]))

    for f in episodes_meta_files(root):
        t = pq.read_table(f)
        idxs = t["episode_index"].to_pylist()
        new_tasks, cfgs, quality = [], [], []
        for i in idxs:
            e = by_index.get(i, {})
            new_tasks.append([e.get("task_text") or ""])
            cfgs.append([{
                "start_frame": s["start_frame"], "end_frame": s["end_frame"],
                "action_text": s.get("text", ""), "skill": s.get("skill", ""),
            } for s in e.get("segments", [])])
            quality.append(e.get("pass_status", "unlabeled"))

        def set_col(tbl, name, arr):
            if name in tbl.column_names:
                tbl = tbl.drop_columns([name])
            return tbl.append_column(name, arr)

        t = set_col(t, "tasks", pa.array(new_tasks, type=pa.list_(pa.string())))
        t = set_col(t, "action_config", pa.array(cfgs, type=seg_type))
        t = set_col(t, "quality", pa.array(quality, type=pa.string()))
        pq.write_table(t, f)

    # data parquet task_index refers to meta/tasks.parquet; keep in sync only
    # when every episode has a single task text (v1: episode-level text lives in
    # 'tasks'; per-frame task_index rewrite is deferred)
    info = load_info(root)
    info["total_tasks"] = len(tasks)
    (root / "meta/info.json").write_text(json.dumps(info, indent=4))

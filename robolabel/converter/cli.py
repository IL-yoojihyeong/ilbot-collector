"""CLI: convert one or more Agibot G2 episode directories into a LeRobot v3.0 dataset.

    robolabel-convert EPISODE_DIR [EPISODE_DIR ...] --out DATASET_DIR \
        [--cameras head_color,hand_left_color,hand_right_color] [--task TEXT]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from . import video
from .agibot import G2Episode
from .writer import EpisodeData, LeRobotWriter, _img_stats

DEFAULT_CAMERAS = ["head_color", "hand_left_color", "hand_right_color"]

# Agibot camera name -> lerobot video key
def video_key(cam: str) -> str:
    return f"observation.images.{cam.removesuffix('_color')}"


def convert(episode_dirs: list[Path], out: Path, cameras: list[str],
            task: str = "", crf: int = 23, append: bool = False,
            log=print) -> Path:
    episodes = [G2Episode(d) for d in episode_dirs]

    first = episodes[0]
    for cam in cameras:
        if cam not in first.available_cameras:
            raise SystemExit(f"camera '{cam}' not in episode "
                             f"(available: {first.available_cameras})")

    fps = first.fps
    video_info = {}
    for cam in cameras:
        w, h = video.dimensions(first.camera_stream(cam), input_format="hevc")
        video_info[video_key(cam)] = {"width": w, "height": h}

    out = Path(out)
    if append and out.exists() and any(out.iterdir()):
        writer = LeRobotWriter.resume(out)
        # 기존 데이터셋과 구성이 같아야 이어 붙일 수 있다.
        # fps는 카메라 타임스탬프 실측값이라 에피소드마다 ±소수점 지터가 있음
        # (예: 30.0 vs 29.82) → 1fps 이내면 기존 데이터셋 fps로 이어 붙인다.
        if abs(float(writer.fps) - float(fps)) > 1.0:
            raise ValueError(f"append 불가: fps 불일치 ({writer.fps} != {fps})")
        if float(writer.fps) != float(fps):
            log(f"append: fps 실측 {fps} → 데이터셋 fps {writer.fps}로 정렬")
        if writer.joint_names != first.joint_names:
            raise ValueError("append 불가: 관절 구성이 기존 데이터셋과 다름")
        if writer.video_info != video_info:
            raise ValueError(f"append 불가: 카메라 구성/해상도 불일치 "
                             f"({sorted(writer.video_info)} != {sorted(video_info)})")
        log(f"append: 기존 {len(writer.episodes)}개 에피소드에 이어 붙임")
    else:
        writer = LeRobotWriter(
            root=out, fps=fps,
            robot_type=str(first.meta.get("robot_type", "agibot_g2")).lower(),
            joint_names=first.joint_names,
            video_info=video_info,
        )

    with tempfile.TemporaryDirectory(prefix="robolabel_") as tmp:
        for i, ep in enumerate(episodes):
            t0 = time.time()
            vids, vstats = {}, {}
            for cam in cameras:
                idx = ep.aligned_frame_indices(cam)
                dst = Path(tmp) / f"ep{i}_{cam}.mp4"
                n = video.encode_h265_frames(ep.camera_stream(cam), dst, idx, fps, crf=crf)
                if n != ep.n_frames:
                    raise RuntimeError(
                        f"{ep.path.name}/{cam}: encoded {n} frames, expected {ep.n_frames}")
                key = video_key(cam)
                vids[key] = dst
                vstats[key] = _img_stats(video.sample_frames(dst, n=32))
            ep_task = task or str(ep.meta.get("text", ""))
            writer.add_episode(EpisodeData(
                state=ep.state, action=ep.action, task=ep_task,
                videos=vids, video_stats=vstats, source_path=str(ep.path),
            ))
            log(f"[{i + 1}/{len(episodes)}] {ep.path.name}: "
                f"{ep.n_frames} frames, {len(cameras)} cams in {time.time() - t0:.1f}s")

    writer.finalize(extra_info={
        "source_format": "agibot_g2",
        "description": f"Converted from Agibot G2 by robolabel ({len(writer.episodes)} episodes)",
    })
    log(f"done -> {out}")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("episodes", nargs="+", type=Path, help="Agibot G2 episode directories")
    p.add_argument("--out", type=Path, required=True, help="output LeRobot dataset directory")
    p.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS),
                   help="comma-separated Agibot camera names")
    p.add_argument("--task", default="", help="task text applied to all episodes")
    p.add_argument("--crf", type=int, default=23, help="x264 quality (lower = better)")
    args = p.parse_args(argv)
    try:
        convert(args.episodes, args.out, [c.strip() for c in args.cameras.split(",") if c.strip()],
                task=args.task, crf=args.crf)
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()

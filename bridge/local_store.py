"""로컬 저장 모드: 에피소드를 노트북의 LeRobot 데이터셋에 append 저장.

플랫폼의 G2→LeRobot 컨버터(robolabel.converter — pyproject 의존성)를 그대로
재사용하므로 서버 모드와 산출 포맷이 완전히 동일하다. 데이터셋은 이름 단위로
여러 세션에 걸쳐 누적되며(writer resume), 에피소드마다 finalize되어
GUI가 죽어도 항상 유효한 상태를 유지한다.

디렉토리 구조: <data_dir>/datasets/<이름>/ (LeRobot v3), <data_dir>/raw/<uuid>/
"""
import os
import shutil
from pathlib import Path

from .config import Config


def dataset_root(cfg: Config, name: str | None = None) -> Path:
    return Path(cfg.local.data_dir).expanduser() / "datasets" / (name or cfg.local.dataset)


def raw_root(cfg: Config) -> Path:
    return Path(cfg.local.data_dir).expanduser() / "raw"


def list_datasets(cfg: Config) -> list[str]:
    base = Path(cfg.local.data_dir).expanduser() / "datasets"
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir()
                  if (d / "meta" / "info.json").exists())


def dataset_summary(cfg: Config, name: str) -> dict | None:
    """{episodes, frames} — GUI 표시용."""
    import json
    info_p = dataset_root(cfg, name) / "meta" / "info.json"
    if not info_p.exists():
        return None
    info = json.loads(info_p.read_text())
    return {"episodes": info.get("total_episodes", 0),
            "frames": info.get("total_frames", 0)}


def store_episode(cfg: Config, local_ep: str, uuid: str, task: str,
                  log=print) -> dict:
    """staging 에피소드를 raw 보관(keep_raw 시) + 데이터셋 append 변환.

    반환: {ep_dir(변환 입력으로 쓴 최종 위치), dataset, n_episodes, n_frames}
    """
    from robolabel.converter.cli import convert          # 지연 import (기동 속도)
    from robolabel.server import lerobot

    name = cfg.local.dataset.strip()
    if not name:
        raise ValueError("로컬 데이터셋 이름이 비어 있습니다 (GUI에서 지정)")

    # ① raw 보관: staging → <data_dir>/raw/<uuid> 로 이동해 그 자리를 변환 입력으로 사용
    ep_dir = Path(local_ep)
    if cfg.local.keep_raw:
        dst = raw_root(cfg) / uuid
        if dst.exists():
            raise FileExistsError(f"raw 보관 위치가 이미 존재: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(ep_dir), str(dst))
        ep_dir = dst
        log(f"raw 보관: {dst}")

    # ② LeRobot append 변환 (서버 모드와 동일 컨버터)
    out = dataset_root(cfg, name)
    out.mkdir(parents=True, exist_ok=True)
    convert([ep_dir], out, cfg.recording.cameras, task=task, append=True, log=log)

    # ③ 검증: 방금 붙인 에피소드가 스캔되는지
    eps = lerobot.scan_episodes(out)
    last = eps[-1]
    return {"ep_dir": str(ep_dir), "dataset": str(out),
            "n_episodes": len(eps), "n_frames": last["length"]}


def list_episodes(cfg: Config, name: str) -> list[dict]:
    """데이터셋의 에피소드 목록 (GUI '에피소드 목록' 창용).

    source_path가 변환 입력으로 쓴 raw 디렉토리를 가리키므로, 변환본 → raw
    역추적이 데이터 차원에서 보장된다 (meta/episodes parquet에 영구 기록).
    """
    from robolabel.server import lerobot
    root = dataset_root(cfg, name)
    if not (root / "meta" / "info.json").exists():
        return []
    return [{"index": e["episode_index"], "length": e["length"],
             "task": e["task"], "raw": e.get("source_path", "")}
            for e in lerobot.scan_episodes(root)]


def local_manifest(ep_dir: str) -> dict[str, int]:
    """로봇 원본 정리 검증용 {relpath: size} (서버 manifest와 동일 형식)."""
    out = {}
    for root, _, files in os.walk(ep_dir):
        for fn in files:
            full = os.path.join(root, fn)
            out[os.path.relpath(full, ep_dir)] = os.path.getsize(full)
    return out

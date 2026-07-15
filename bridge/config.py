"""Bridge configuration — loads config.json at repo root (gitignored).

config.example.json documents the schema; copy it to config.json and fill in
credentials. Missing keys fall back to the defaults below.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "vendor"
BRIDGE_VERSION = "0.1.0"          # 릴리스 태그와 맞춤 (meta sw_version·GUI 타이틀)

DEFAULT_TOPICS = [
    "/hal/joint_state", "/hal/joint_cmd", "/hal/left_ee_data",
    "/hal/right_ee_data", "/hal/usr_state", "/hal/whole_body_status",
    "/hal/chassis_joint_state", "/genie/bundle_data", "/tf",
    "/dr/odom", "/imu/chassis",
    "/remote/vr_data", "/remote/vr_network",
    "/wbc/arm_command", "/wbc/gripper_command",
    "/wbc/left_ee_command", "/wbc/right_ee_command", "/wbc/retarget",
]
# pbdat files the laptop needs to build aligned_joints.h5
H5_INPUT_PBDATS = [
    "-hal-joint_state.pbdat", "-hal-joint_cmd.pbdat",
    "-hal-left_ee_data.pbdat", "-hal-right_ee_data.pbdat",
]


@dataclass
class ServerCfg:
    api_url: str = "http://100.118.52.30:8000"
    api_user: str = ""          # HTTP Basic (ROBOLABEL_PASSWORD); empty = no auth
    api_password: str = ""
    # 에피소드 전송 방식: "http"(권장, 플랫폼 API로 tar 업로드 — ssh_* 불필요)
    # | "sftp"(레거시, 서버 SSH로 직접 push)
    transport: str = "sftp"
    ssh_host: str = "100.118.52.30"
    ssh_user: str = "yoo"
    ssh_password: str = ""
    # raw episodes live here (inside ROBOLABEL_DATA so raw + converted are managed together)
    incoming_dir: str = "/home/yoo/robolabel_data/raw"


@dataclass
class RobotCfg:
    ssh_host: str = "10.42.1.101"
    ssh_user: str = "agi"
    ssh_password: str = ""
    record_root: str = "/data/record"
    env_prefix: str = "source /home/agi/app/env.sh >/dev/null 2>&1"
    ctl_dir: str = "/tmp/robolabel_bridge"


@dataclass
class RecordingCfg:
    # 미사용(구 record_ctl 시절 잔재) — 실제 토픽은 데몬이 로봇 record_topic.json 전체 사용.
    # 기존 config.json 호환을 위해 필드만 유지.
    topics: list = field(default_factory=lambda: list(DEFAULT_TOPICS))
    cameras: list = field(default_factory=lambda: ["head_color", "hand_left_color", "hand_right_color"])
    post_win_ms: int = 600000
    ref_cam: str = "head_color"
    pull_all_pbdat: bool = True    # raw archive: pull everything the robot recorded
    push_pbdat: bool = True        # raw archive: push everything to the server
    cleanup_robot: bool = True     # delete robot original after server copy is size-verified
    countdown_s: int = 3


@dataclass
class DaemonCfg:
    port: int = 18800
    idle_timeout_s: int = 900      # daemon self-exits after this without heartbeats (if idle)
    stream_cams: list = field(default_factory=lambda: ["head_color", "hand_left_color", "hand_right_color"])
    stream_fps: float = 4.0


@dataclass
class LocalCfg:
    """서버 없이 노트북에 저장하는 로컬 모드 설정 (mode="local")."""
    data_dir: str = str(Path.home() / "ilbot_data")  # datasets/<이름>, raw/<uuid>
    dataset: str = ""              # 누적할 LeRobot 데이터셋 이름 (GUI에서 선택/입력)
    keep_raw: bool = True          # raw(40토픽+카메라 원본)를 노트북에 보관


@dataclass
class Config:
    # "server": 플랫폼으로 업로드(기존) | "local": 노트북에 LeRobot으로 직접 저장
    mode: str = "server"
    server: ServerCfg = field(default_factory=ServerCfg)
    robot: RobotCfg = field(default_factory=RobotCfg)
    recording: RecordingCfg = field(default_factory=RecordingCfg)
    daemon: DaemonCfg = field(default_factory=DaemonCfg)
    local: LocalCfg = field(default_factory=LocalCfg)
    staging_dir: str = str(Path.home() / "robolabel_staging")
    robot_type: str = "G2A"
    robot_aid: str = "G2A0004BC00489"


def load(path: Path | None = None) -> Config:
    path = path or REPO_ROOT / "config.json"
    cfg = Config()
    if path.exists():
        raw = json.loads(path.read_text())
        for section, cls in (("server", ServerCfg), ("robot", RobotCfg),
                             ("recording", RecordingCfg), ("daemon", DaemonCfg),
                             ("local", LocalCfg)):
            if section in raw:
                cur = getattr(cfg, section)
                for k, v in raw[section].items():
                    if hasattr(cur, k):
                        setattr(cur, k, v)
        for k in ("staging_dir", "robot_type", "robot_aid", "mode"):
            if k in raw:
                setattr(cfg, k, raw[k])
    return cfg


def save_user_prefs(cfg: Config, path: Path | None = None):
    """GUI에서 바뀌는 값(mode, local.dataset)을 config.json에 되써준다."""
    path = path or REPO_ROOT / "config.json"
    raw = json.loads(path.read_text()) if path.exists() else {}
    raw["mode"] = cfg.mode
    raw.setdefault("local", {})
    raw["local"]["data_dir"] = cfg.local.data_dir
    raw["local"]["dataset"] = cfg.local.dataset
    raw["local"]["keep_raw"] = cfg.local.keep_raw
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2))


def save_settings(cfg: Config, path: Path | None = None):
    """GUI 설정 창의 필수 값(서버·로봇 접속)을 config.json에 기록.

    파일이 없으면 새로 만든다 — 첫 실행 설정 마법사가 이 함수로 config를 생성.
    """
    path = path or REPO_ROOT / "config.json"
    raw = json.loads(path.read_text()) if path.exists() else {}
    raw["mode"] = cfg.mode
    s = raw.setdefault("server", {})
    s["api_url"] = cfg.server.api_url
    s["api_user"] = cfg.server.api_user
    s["api_password"] = cfg.server.api_password
    s["transport"] = cfg.server.transport
    r = raw.setdefault("robot", {})
    r["ssh_host"] = cfg.robot.ssh_host
    r["ssh_user"] = cfg.robot.ssh_user
    r["ssh_password"] = cfg.robot.ssh_password
    lo = raw.setdefault("local", {})
    lo.setdefault("data_dir", cfg.local.data_dir)
    lo.setdefault("dataset", cfg.local.dataset)
    lo.setdefault("keep_raw", cfg.local.keep_raw)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2))


def config_exists(path: Path | None = None) -> bool:
    return (path or REPO_ROOT / "config.json").exists()

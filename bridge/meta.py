"""meta_info.json generator — minimal fields proven to pass the converter."""
import json
from datetime import datetime

from .config import BRIDGE_VERSION


def build_meta(uuid: str, duration_s: int, description: str,
               robot_type: str = "G2A", aid: str = "",
               recorder: str = "", job_name: str = "",
               gdk_version: str = "") -> dict:
    return {
        # provenance: 어떤 GDK가 이 에피소드를 기록했는지 (버전별 이슈 추적용)
        **({"gdk_version": gdk_version} if gdk_version else {}),
        "AID": aid,
        "author": recorder or "robolabel_bridge",
        "create_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration": int(duration_s),
        "ee_list": [
            {"name": "left_hand", "type": "zhiyuan_gripper_omnipicker"},
            {"name": "right_hand", "type": "zhiyuan_gripper_omnipicker"},
        ],
        "episode_token": uuid,
        "robot_type": robot_type,
        "sw_version": f"ilbot_bridge_{BRIDGE_VERSION}",
        "task_mode": "TDC",
        "text": json.dumps({"description": description, "job": job_name},
                           ensure_ascii=False),
        "version": "v0.0.2",
    }


def write_meta(path, **kwargs):
    meta = build_meta(**kwargs)
    with open(path, "w") as f:
        json.dump(meta, f, indent=4, ensure_ascii=False)
    return meta

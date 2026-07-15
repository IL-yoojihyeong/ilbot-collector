"""로봇/서버 없이 도는 브리지 유닛 테스트 (CI용).

실행: uv run pytest tests/ -q   (PyQt/GUI·SSH 경로는 다루지 않음)
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import config as cfg_mod
from bridge.api_client import _ProgressFile
from bridge.meta import build_meta
from bridge.robot_profile import REQUIRED_FIELDS, proto_fields


def test_config_defaults_load_without_file(tmp_path):
    cfg = cfg_mod.load(tmp_path / "none.json")
    assert cfg.server.transport in ("sftp", "http")
    assert cfg.recording.cleanup_robot is True
    assert cfg.recording.ref_cam == "head_color"
    assert cfg.mode == "server" and cfg.local.keep_raw is True


def test_step_name_sets_same_length():
    from bridge import pipeline
    assert len(pipeline.STEPS) == len(pipeline.STEPS_LOCAL)     # 큐 step_states 호환
    assert pipeline.step_names("local") is pipeline.STEPS_LOCAL
    assert pipeline.step_names("server") is pipeline.STEPS


def test_save_user_prefs_roundtrip(tmp_path):
    p = tmp_path / "config.json"
    cfg = cfg_mod.load(p)
    cfg.mode = "local"
    cfg.local.dataset = "ds1"
    cfg_mod.save_user_prefs(cfg, p)
    cfg2 = cfg_mod.load(p)
    assert cfg2.mode == "local" and cfg2.local.dataset == "ds1"


def test_progress_file_reports_and_len():
    data = b"x" * 1000
    calls = []
    pf = _ProgressFile(io.BytesIO(data), len(data), lambda s, t: calls.append((s, t)))
    out = b""
    while True:
        chunk = pf.read(256)
        if not chunk:
            break
        out += chunk
    assert out == data and len(pf) == 1000
    assert calls[-1] == (1000, 1000)


def test_build_meta_provenance_fields():
    m = build_meta(uuid="u1", duration_s=5, description="d", aid="AID1",
                   gdk_version="2.3.4")
    assert m["gdk_version"] == "2.3.4" and m["AID"] == "AID1"
    m2 = build_meta(uuid="u1", duration_s=5, description="d")
    assert "gdk_version" not in m2          # 미탐지 시 키 자체를 넣지 않음


def test_proto_fields_extracts_required_from_vendored():
    vendor = Path(__file__).resolve().parent.parent / "vendor"
    for rel, msgs in REQUIRED_FIELDS.items():
        fields = proto_fields((vendor / rel).read_text())
        for msg, names in msgs.items():
            have = {f[1] for f in fields[msg]}
            assert set(names) <= have, (rel, msg, names, have)

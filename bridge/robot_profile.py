"""로봇 프로파일: GDK 버전 탐지 → proto 호환 판정 → 버전별 proto 캐시.

어떤 GDK 버전이든 소화하기 위한 계층. 원리:
- 로봇측(데몬)은 로봇 자신의 GDK를 import하므로 항상 버전 일치 — 문제 없음.
- 노트북측에서 버전에 묶이는 건 h5 빌더의 pbdat 파싱(JointState/JointCommand)뿐.
  vendored proto(기준: GDK 2.6.3)로 파싱 가능한지 **필드 번호/타입 레벨**로 판정하고,
  불가능하면 로봇의 pb2를 버전별로 캐시해 그걸로 파싱한다(h5_builder 서브프로세스).
- 판정 결과·버전은 프로파일로 캐시되고 meta_info.json에 provenance로 기록된다.

프로파일 dict:
  {gdk_version, gdk_commit, aid, robot_hostname, n_topics,
   proto_verdict: identical|compatible|robot_protos, proto_dir(옵션),
   detected_at}
"""
import ast
import hashlib
import json
import os
import re
import time
from pathlib import Path

from .config import VENDOR_DIR

ROBOT_GDK_LIB = "/home/agi/app/gdk/lib"
CACHE_ROOT = Path.home() / ".cache" / "robolabel_bridge" / "protos"
PROFILE_DIR = Path.home() / "robolabel_staging" / "robot_profiles"

# 노트북 파싱이 실제로 읽는 필드 — 이것들이 같은 번호/타입으로 존재하면
# vendored proto로 어떤 버전의 pbdat도 안전하게 파싱할 수 있다(와이어 호환).
REQUIRED_FIELDS = {
    "genie_msgs_pb/msg/JointState_pb2.py": {
        "JointState": ["name", "position", "motor_position"]},
    "genie_msgs_pb/msg/JointCommand_pb2.py": {
        "JointCommand": ["name", "position"]},
    "genie_msgs_pb/msg/EndState_pb2.py": {          # /hal/*_ee_data (그리퍼)
        "EndState": ["name", "end_state"]},
}
# 캐시는 로봇 gdk/lib의 *_pb 패키지 전체를 가져온다 — 각 패키지 __init__가
# star-import로 넓은 의존 트리(geometry, tf2 등)를 끌어오기 때문.


def proto_fields(src: str) -> dict:
    """pb2 소스에서 message별 {(number, name, type)} 집합 추출."""
    from google.protobuf import descriptor_pb2
    m = re.search(r"AddSerializedFile\(\s*(b'(?:[^'\\]|\\.)*')\s*\)", src, re.S)
    if not m:
        return {}
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.ParseFromString(ast.literal_eval(m.group(1)))
    return {msg.name: {(f.number, f.name, f.type) for f in msg.field}
            for msg in fdp.message_type}


def _proto_verdict(ssh) -> tuple[str, list[str]]:
    """vendored proto로 로봇 데이터를 파싱할 수 있는지 판정.

    identical  — 파일 md5까지 동일
    compatible — 다르지만 REQUIRED_FIELDS가 같은 번호/타입으로 존재
    robot_protos — vendored로는 위험 → 로봇 proto 사용 필요
    """
    verdict, notes = "identical", []
    for rel, msgs in REQUIRED_FIELDS.items():
        lp = VENDOR_DIR / rel
        rc, out, _ = ssh.run(f"md5sum {ROBOT_GDK_LIB}/{rel} 2>/dev/null")
        if rc != 0 or not out.strip():
            return "robot_protos", [f"{rel}: 로봇에 없음 — 경로/구조 변경"]
        if out.split()[0] == hashlib.md5(lp.read_bytes()).hexdigest():
            continue
        rc, robot_src, _ = ssh.run(f"cat {ROBOT_GDK_LIB}/{rel}")
        ours, theirs = proto_fields(lp.read_text()), proto_fields(robot_src)
        for msg, fields in msgs.items():
            our_by_name = {f[1]: f for f in ours.get(msg, set())}
            their_by_name = {f[1]: f for f in theirs.get(msg, set())}
            for fname in fields:
                if fname not in their_by_name:
                    notes.append(f"{msg}.{fname}: 로봇 proto에 없음")
                    return "robot_protos", notes
                if our_by_name.get(fname) != their_by_name[fname]:
                    notes.append(f"{msg}.{fname}: 번호/타입 변경 "
                                 f"{our_by_name.get(fname)} → {their_by_name[fname]}")
                    return "robot_protos", notes
        verdict = "compatible"
        notes.append(f"{rel}: 스키마 다르지만 필요 필드는 동일 번호/타입")
    return verdict, notes


def fetch_protos(ssh, cache_key: str, log=print) -> Path:
    """로봇의 pb2 패키지들을 버전별 캐시 디렉토리로 복사(있으면 재사용)."""
    dst = CACHE_ROOT / cache_key
    if (dst / "genie_msgs_pb").is_dir():
        return dst
    log(f"로봇 proto 캐시 생성: {dst}")
    dst.mkdir(parents=True, exist_ok=True)
    rc, out, _ = ssh.run(f"ls {ROBOT_GDK_LIB} | grep '_pb$'")
    for pkg in out.split():
        ssh.get_tree(f"{ROBOT_GDK_LIB}/{pkg}", str(dst / pkg),
                     name_filter=lambda p: p.endswith(".py"))
    return dst


def _gdk_version(ssh) -> tuple[str, str]:
    rc, out, _ = ssh.run("curl -s -m 3 http://127.0.0.1:8849/gdk_version")
    try:
        v = json.loads(out)
        return str(v.get("Version", "unknown")), str(v.get("Commit", ""))
    except Exception:
        return "unknown", ""


def _detect_aid(ssh) -> str:
    rc, out, _ = ssh.run(
        "grep -rhoE 'G2[A-Z0-9]{10,}' /data/version /home/agi/app/conf/deploy "
        "2>/dev/null | head -1")
    return out.strip()


def ensure(ssh, log=print) -> dict:
    """열려 있는 로봇 SSH 세션으로 프로파일 확보 (버전 같으면 캐시 재사용).

    반환 프로파일의 proto_dir: None이면 vendored 사용, 경로면 그 proto로
    h5를 빌드해야 함(h5_builder.build_auto가 서브프로세스로 처리).
    """
    version, commit = _gdk_version(ssh)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    host = ssh.client.get_transport().getpeername()[0]
    cache_file = PROFILE_DIR / f"{host.replace('.', '_')}.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            if (cached.get("gdk_version"), cached.get("gdk_commit")) == (version, commit):
                return cached
        except Exception:
            pass

    verdict, notes = _proto_verdict(ssh)
    proto_dir = None
    if verdict == "robot_protos":
        proto_dir = str(fetch_protos(ssh, f"{version}-{commit or 'x'}", log=log))
    rc, out, _ = ssh.run("hostname")
    profile = {
        "gdk_version": version, "gdk_commit": commit,
        "aid": _detect_aid(ssh), "robot_hostname": out.strip(),
        "proto_verdict": verdict, "proto_notes": notes, "proto_dir": proto_dir,
        "detected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    cache_file.write_text(json.dumps(profile, ensure_ascii=False, indent=1))
    log(f"로봇 프로파일: GDK {version} ({commit}) · proto={verdict}"
        + (f" · {proto_dir}" if proto_dir else ""))
    for n in notes:
        log(f"  · {n}")
    return profile

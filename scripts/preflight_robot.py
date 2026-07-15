#!/usr/bin/env python3
"""새 로봇 연결 시 GDK 호환성 사전 점검 (노트북에서 실행).

브리지가 의존하는 로봇측 요소를 전부 확인하고 OK/경고를 리포트한다:
GDK 버전, dds_record 데몬, record_topic.json, proto 바인딩(vendored와 diff),
agibot_gdk 임포트, env.sh, 카메라 설정, 디스크 여유, 로봇 식별 정보.

사용: .venv/bin/python scripts/preflight_robot.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge import config as cfg_mod
from bridge.ssh_link import SSHSession

from bridge import robot_profile

ok_n = warn_n = 0


def report(ok: bool, msg: str):
    global ok_n, warn_n
    print(("  ✅ " if ok else "  ⚠️  ") + msg)
    ok_n += ok
    warn_n += not ok


def main():
    cfg = cfg_mod.load()
    print(f"로봇 {cfg.robot.ssh_host} 점검 중...\n")
    try:
        s = SSHSession(cfg.robot.ssh_host, cfg.robot.ssh_user,
                       cfg.robot.ssh_password, timeout=5)
    except Exception as e:
        sys.exit(f"  ❌ SSH 접속 실패: {e}\n     유선 연결/IP({cfg.robot.ssh_host})/계정을 확인하세요")
    report(True, f"SSH 접속 OK ({cfg.robot.ssh_user}@{cfg.robot.ssh_host})")

    # --- 로봇 식별 + GDK 버전
    rc, out, _ = s.run("hostname; uname -m; "
                       "cat /data/version 2>/dev/null | head -3")
    print(f"     호스트: {' / '.join(out.split()[:2])}")
    rc, out, _ = s.run("curl -s -m 3 http://127.0.0.1:8849/gdk_version")
    try:
        v = json.loads(out)
        report(True, f"GDK {v.get('Version')} (commit {v.get('Commit')}, "
                     f"build {v.get('BuildTime')}) — 기준 검증 버전: 2.6.3/91b3a5d")
        if v.get("Version") != "2.6.3":
            report(False, "GDK 버전이 검증본(2.6.3)과 다름 — 아래 proto diff 결과를 주의 깊게 볼 것")
    except Exception:
        report(False, "gdk_http_server(:8849) 응답 없음 — GDK 버전 확인 불가")

    # --- dds_record 데몬 + record_topic.json
    rc, out, _ = s.run("pgrep -a dds_record | head -2")
    report(bool(out.strip()), f"dds_record 데몬: {out.strip() or '실행 안 됨(!)'}")
    rc, out, _ = s.run(
        "python3 -c \"import json;"
        "d=json.load(open('/home/agi/app/conf/record/record_topic.json'));"
        "print(len(d.get('record_props', d) if isinstance(d, dict) else d))\" "
        "2>/dev/null || echo FAIL")
    report(out.strip() != "FAIL",
           f"record_topic.json 토픽 수: {out.strip()} (2.6.3 기준 40)")

    # --- 핵심 proto 호환 판정 (robot_profile과 동일 로직 = 실제 파이프라인 동작)
    verdict, notes = robot_profile._proto_verdict(s)
    if verdict == "identical":
        report(True, "핵심 proto: vendored와 완전 일치")
    elif verdict == "compatible":
        report(True, "핵심 proto: 필드 호환 — vendored로 파싱 (h5 인프로세스)")
    else:
        report(True, "핵심 proto: 비호환 — 파이프라인이 로봇 proto를 자동 캐시해 사용")
        print("       (첫 수집 후 h5·변환 결과를 한 번 검수할 것)")
    for n in notes:
        print(f"       · {n}")

    # --- env.sh + agibot_gdk 임포트 (env.sh 인자 함정 회피: set -- 후 source)
    rc, out, err = s.run(
        'bash -c "set --; source /home/agi/app/env.sh >/dev/null 2>&1; '
        'python3 -c \'import agibot_gdk\' && echo IMPORT_OK" 2>&1')
    report("IMPORT_OK" in out, f"agibot_gdk 임포트: "
           f"{'OK' if 'IMPORT_OK' in out else (out.strip().splitlines() or ['실패'])[-1][:120]}")

    # --- 카메라: 배포 설정(conf/deploy/camera_*)에 카메라명이 정의돼 있는지
    for cam in cfg.recording.cameras:
        rc, out, _ = s.run(
            f"grep -rl '{cam}' /home/agi/app/conf/deploy/camera* 2>/dev/null | head -1")
        hit = bool(out.strip())
        report(hit, f"카메라 '{cam}' {'정의 발견: ' + out.strip().split('/')[-2] if hit else '배포 설정에서 못 찾음 — 유효 카메라명 확인 필요'}")

    # --- 카메라 런타임 프레임: 설정에 있어도 실제 프레임이 나오는지
    # (GDK get_latest_image; 단독 프로세스라 Camera+DDS Node 데드락과 무관)
    probe = """
import sys, time
sys.path.insert(0, "/home/agi/app/gdk/lib")
sys.path.insert(0, "/home/agi/app/local/lib/python3.10/dist-packages")
import agibot_gdk
cam = agibot_gdk.Camera()
time.sleep(2.0)
for name in sys.argv[1:]:
    ct = getattr(agibot_gdk.CameraType,
                 "k" + "".join(w.capitalize() for w in name.split("_")))
    got = False
    for _ in range(3):
        try:
            img = cam.get_latest_image(ct, 1000.0)
            if img is not None and bytes(img.data):
                got = True
                break
        except Exception:
            pass
    print(("FRAME_OK " if got else "FRAME_FAIL ") + name)
"""
    with s.sftp.open("/tmp/robolabel_preflight_cam.py", "w") as f:
        f.write(probe)
    rc, out, _ = s.run(
        'bash -c "set --; source /home/agi/app/env.sh >/dev/null 2>&1; '
        f'python3 /tmp/robolabel_preflight_cam.py {" ".join(cfg.recording.cameras)}" 2>/dev/null',
        timeout=90)
    for cam_name in cfg.recording.cameras:
        if f"FRAME_OK {cam_name}" in out:
            report(True, f"카메라 '{cam_name}' 실프레임 OK")
        else:
            report(False, f"카메라 '{cam_name}' 프레임 안 나옴 — 케이블/카메라 서비스 확인 필요")

    # --- 저장 경로 + 디스크
    rc, out, _ = s.run(f"test -d {cfg.robot.record_root} && "
                       f"df -h {cfg.robot.record_root} | tail -1")
    report(rc == 0, f"{cfg.robot.record_root}: {out.strip() or '없음(!)'}")

    # --- AID (config.json robot_aid 갱신 안내)
    rc, out, _ = s.run("grep -rhoE 'G2[A-Z0-9]{10,}' /data/version /home/agi/app/conf/deploy 2>/dev/null | head -1")
    aid = out.strip()
    if aid and aid != cfg.robot_aid:
        report(False, f"로봇 AID {aid} ≠ config.json robot_aid({cfg.robot_aid}) — config 갱신 필요")
    elif aid:
        report(True, f"AID {aid} (config와 일치)")
    else:
        print("     AID 자동 탐지 실패 — meta_info 생성 전 config.json robot_aid 수동 확인")

    s.close()
    print(f"\n결과: OK {ok_n} / 경고 {warn_n}")
    if warn_n:
        print("경고 항목을 해결한 뒤 GUI 워밍업 → 짧은 테스트 녹화(scripts/e2e_test.py --seconds 8)로 검증하세요.")
    else:
        print("바로 e2e 테스트 가능: .venv/bin/python scripts/e2e_test.py --seconds 8")


if __name__ == "__main__":
    main()

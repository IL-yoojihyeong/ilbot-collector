#!/usr/bin/env python3
"""로봇 /data/record 의 과거 에피소드 일괄 삭제 도구.

기본은 dry-run(목록·용량만 표시). 실제 삭제는 --yes 필요.
안전장치:
  - 최근 --min-age-min 분 내에 수정된 디렉토리는 건너뜀 (녹화/회수 중 보호)
  - 업로드 큐(upload_queue.json)에서 done이 아닌 uuid는 건너뜀
  - --keep 으로 보존할 uuid(부분 일치) 지정 가능

사용:
  .venv/bin/python scripts/purge_robot_records.py            # dry-run
  .venv/bin/python scripts/purge_robot_records.py --yes      # 실제 삭제
  .venv/bin/python scripts/purge_robot_records.py --keep 3f2b8c41 --yes
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge import config as cfg_mod
from bridge.ssh_link import SSHSession


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="실제로 삭제 (없으면 dry-run)")
    ap.add_argument("--keep", nargs="*", default=[], help="보존할 uuid (부분 일치)")
    ap.add_argument("--min-age-min", type=int, default=30,
                    help="이 시간(분) 내 수정된 디렉토리는 건너뜀 (기본 30)")
    args = ap.parse_args()

    cfg = cfg_mod.load()
    root = cfg.robot.record_root

    # 업로드 큐에서 아직 서버로 안 넘어간 uuid 수집
    queue_path = Path(cfg.staging_dir).expanduser() / "upload_queue.json"
    in_flight = set()
    if queue_path.exists():
        for j in json.loads(queue_path.read_text()):
            if j.get("status") != "done":
                in_flight.add(j["uuid"])

    ssh = SSHSession(cfg.robot.ssh_host, cfg.robot.ssh_user, cfg.robot.ssh_password)
    try:
        rc, out, err = ssh.run(
            f"find '{root}' -mindepth 1 -maxdepth 1 -type d -printf '%f\\t%T@\\n'")
        if rc != 0:
            sys.exit(f"목록 조회 실패: {err.strip()}")
        entries = [(name, float(mtime)) for name, _, mtime in
                   (l.partition("\t") for l in out.splitlines()) if name]

        rc, du_out, _ = ssh.run(f"du -s --block-size=1M '{root}'/*/ 2>/dev/null", timeout=300)
        sizes = {}  # dirname -> MB
        for line in du_out.splitlines():
            mb, _, path = line.partition("\t")
            sizes[path.rstrip("/").rsplit("/", 1)[-1]] = int(mb)

        now = time.time()
        targets, skipped = [], []
        for name, mtime in sorted(entries, key=lambda e: e[1]):
            age_min = (now - mtime) / 60
            if any(k in name for k in args.keep):
                skipped.append((name, "keep 지정"))
            elif name in in_flight:
                skipped.append((name, "업로드 큐 미완료"))
            elif age_min < args.min_age_min:
                skipped.append((name, f"최근 수정 {age_min:.0f}분 전"))
            else:
                targets.append(name)

        total_mb = sum(sizes.get(n, 0) for n in targets)
        print(f"대상 {len(targets)}개 (총 {total_mb/1024:.1f} GB) / 건너뜀 {len(skipped)}개")
        for name, why in skipped:
            print(f"  [건너뜀] {name} — {why}")
        for name in targets:
            print(f"  [삭제{'' if args.yes else ' 예정'}] {name} ({sizes.get(name, 0)} MB)")

        if not args.yes:
            print("\ndry-run — 실제 삭제는 --yes 를 붙이세요.")
            return
        if not targets:
            print("삭제할 것이 없습니다.")
            return

        for i, name in enumerate(targets, 1):
            # dds_record writes as root — plain rm gets Permission denied
            rc, _, err = ssh.run_sudo(f"rm -rf '{root}/{name}'",
                                      cfg.robot.ssh_password, timeout=600)
            state = "OK" if rc == 0 else f"실패: {err.strip()[:100]}"
            print(f"  ({i}/{len(targets)}) {name} ... {state}")

        rc, df_out, _ = ssh.run(f"df -h '{root}' | tail -1")
        print(f"\n완료. 디스크 상태: {df_out.strip()}")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()

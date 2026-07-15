#!/usr/bin/env python3
"""End-to-end smoke test without the GUI: record N seconds on the real robot,
run the full pipeline, register to the platform, wait for conversion.

Usage: .venv/bin/python scripts/e2e_test.py [--seconds 8] [--project-id N] [--job-id M]
"""
import argparse
import sys
import time
import uuid as uuidlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import config as cfgmod
from bridge import pipeline
from bridge.api_client import RoboLabelAPI
from bridge.daemon_client import DaemonClient
from bridge.ssh_link import RobotLink


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=8)
    ap.add_argument("--project-id", type=int)
    ap.add_argument("--job-id", type=int)
    ap.add_argument("--local", action="store_true",
                    help="서버 없이 노트북 로컬 LeRobot 데이터셋에 저장 (append)")
    ap.add_argument("--dataset", default="e2e-local-test",
                    help="--local일 때 누적할 데이터셋 이름")
    args = ap.parse_args()

    cfg = cfgmod.load()
    if args.local:
        cfg.mode = "local"
        cfg.local.dataset = args.dataset
        proj = job = None
        print(f"target: 로컬 데이터셋 '{args.dataset}' "
              f"({Path(cfg.local.data_dir).expanduser()}/datasets)")
    else:
        api = RoboLabelAPI(cfg.server)
        projects = api.projects()
        proj = next((p for p in projects if p["id"] == args.project_id), projects[0]) \
            if args.project_id else projects[0]
        jobs = api.jobs(proj["id"])
        if not jobs:
            sys.exit(f"project {proj['name']} has no jobs")
        job = next((j for j in jobs if j["id"] == args.job_id), jobs[0]) \
            if args.job_id else jobs[0]
        print(f"target: project '{proj['name']}' (#{proj['id']}) / job '{job['name']}' (#{job['id']})")

    ep_uuid = str(uuidlib.uuid4())
    print(f"uuid: {ep_uuid}")
    rc = cfg.recording

    daemon = DaemonClient(cfg.robot, cfg.daemon.port)
    st = daemon.ensure_running(cfg.daemon.stream_cams, cfg.daemon.stream_fps,
                               cfg.daemon.idle_timeout_s)
    print(f"daemon up: uptime={st['uptime_s']}s topics={st['n_topics']} "
          f"stream_ready={st['stream_ready']}")

    t0 = time.time()
    res = daemon.start(ep_uuid, rc.post_win_ms, rc.cameras)
    print(f"START ({time.time() - t0:.2f}s):", {k: res[k] for k in ('ok', 'verified', 'n_topics')})
    assert res.get("ok"), f"start failed: {res}"
    print(f"recording {args.seconds}s ...")
    time.sleep(args.seconds)
    t0 = time.time()
    res = daemon.stop(ep_uuid, rc.post_win_ms)
    print(f"STOP ({time.time() - t0:.2f}s): ok={res['ok']}")
    time.sleep(2)

    robot = RobotLink(cfg.robot)
    try:
        assert robot.episode_exists(ep_uuid), "episode dir missing on robot"
    finally:
        robot.close()

    names = pipeline.step_names(cfg.mode)

    def step_cb(i, state):
        print(f"  [{names[i]}] {state}")

    if args.local:
        result = pipeline.run_local(cfg, ep_uuid, args.seconds,
                                    "e2e local smoke test", step_cb=step_cb)
        print("PIPELINE RESULT:", result)
        print(f"dataset '{args.dataset}' now has {result['n_episodes']} episodes")
    else:
        result = pipeline.run(cfg, ep_uuid, proj["id"], job["id"],
                              args.seconds, "e2e smoke test",
                              job_name=job["name"], step_cb=step_cb)
        print("PIPELINE RESULT:", result)
        eps = api.episodes(proj["id"])
        print(f"project now has {len(eps)} episodes")
    print("E2E TEST PASSED")


if __name__ == "__main__":
    main()

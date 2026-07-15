"""Post-stop processing chain: pull → h5 → meta → push → import → poll."""
import shutil
import time

from . import h5_builder, local_store, meta, robot_profile
from .api_client import RoboLabelAPI
from .config import Config
from .ssh_link import RobotLink, ServerLink

STEPS = ["에피소드 회수", "h5 생성", "meta 생성", "서버 업로드", "플랫폼 등록", "변환 대기",
         "로봇 원본 정리"]
# 로컬 저장 모드(run_local) — 큐 호환을 위해 서버 모드와 같은 7단계 길이 유지
STEPS_LOCAL = ["에피소드 회수", "h5 생성", "meta 생성", "raw 보관", "로컬 변환(append)",
               "데이터셋 검증", "로봇 원본 정리"]


def step_names(mode: str) -> list:
    return STEPS_LOCAL if mode == "local" else STEPS


def _pull_and_prepare(cfg: Config, uuid: str, duration_s: int, description: str,
                      recorder: str, job_name: str, step, log):
    """공통 전처리 (스텝 0~2): 회수+프로파일 → h5 → meta. (local_ep, n_frames) 반환."""
    robot = RobotLink(cfg.robot)
    try:
        step(0, "run")
        # GDK 버전 프로파일 (버전 안 바뀌면 캐시 반환) — h5 proto 선택 + provenance
        profile = robot_profile.ensure(robot.ssh, log=log)
        log(f"로봇에서 에피소드 회수 중: {uuid}")
        local_ep = robot.pull_episode(
            uuid, cfg.staging_dir, cfg.recording.pull_all_pbdat,
            progress=lambda p, sz: log(f"  ↓ {p.split('/')[-1]} ({sz/1e6:.1f}MB)"))
        step(0, "ok")
    finally:
        robot.close()

    step(1, "run")
    _, n_frames, warns = h5_builder.build_auto(
        local_ep, cfg.recording.ref_cam, proto_dir=profile.get("proto_dir"), log=log)
    for w in warns:
        log(f"  경고: {w}")
    log(f"aligned_joints.h5 생성: {n_frames}프레임")
    step(1, "ok")

    step(2, "run")
    meta.write_meta(f"{local_ep}/meta_info.json", uuid=uuid, duration_s=duration_s,
                    description=description, robot_type=cfg.robot_type,
                    aid=profile.get("aid") or cfg.robot_aid,
                    recorder=recorder, job_name=job_name,
                    gdk_version=profile.get("gdk_version", ""))
    step(2, "ok")
    return local_ep, n_frames


def run(cfg: Config, uuid: str, project_id: int, job_id: int,
        duration_s: int, description: str, recorder: str = "", job_name: str = "",
        step_cb=None, log=print, poll_timeout=900):
    """Run the full post-recording pipeline. step_cb(i, 'run'|'ok'|'fail')."""

    def step(i, state):
        if step_cb:
            step_cb(i, state)

    local_ep, n_frames = _pull_and_prepare(
        cfg, uuid, duration_s, description, recorder, job_name, step, log)

    api = RoboLabelAPI(cfg.server)
    server_files = None            # http 응답 manifest (7단계 원본 정리 검증용)
    if cfg.server.transport == "http":
        # HTTP 전송: tar 업로드 한 번으로 서버 저장 + import 등록까지 처리됨
        step(3, "run")
        last = {"pct": -10}

        def _prog(sent, total):
            pct = int(sent * 100 / max(1, total))
            if pct >= last["pct"] + 10:
                last["pct"] = pct
                log(f"  ↑ 업로드 {pct}% ({sent/1e6:.0f}/{total/1e6:.0f}MB)")

        res = api.upload_episode(job_id, uuid, local_ep, progress=_prog)
        import_id = res["import_id"]
        server_files = res.get("files") or {}
        remote_path = f"(server) raw/{uuid}"
        log(f"HTTP 업로드 완료 — import #{import_id} (format={res.get('format')})")
        step(3, "ok")
        step(4, "run")
        step(4, "ok")              # 업로드 API가 등록을 겸함
    else:
        step(3, "run")
        server = ServerLink(cfg.server)
        try:
            remote_path = server.push_episode(
                local_ep, uuid, cfg.recording.push_pbdat,
                progress=lambda p, sz: log(f"  ↑ {p.split('/')[-1]} ({sz/1e6:.1f}MB)"))
        finally:
            server.close()
        log(f"서버 업로드 완료: {remote_path}")
        step(3, "ok")

        step(4, "run")
        res = api.create_import(project_id, remote_path, job_id)
        import_id = res["id"]
        log(f"플랫폼 등록: import #{import_id} (format={res.get('format')})")
        step(4, "ok")

    step(5, "run")
    t0 = time.time()
    while time.time() - t0 < poll_timeout:
        st = api.import_status(project_id, import_id)
        if st is None:
            raise RuntimeError("import task disappeared")
        if st["status"] == "done":
            log(f"변환 완료 (dataset_id={st.get('dataset_id')})")
            step(5, "ok")
            # raw is safely on the server now — drop the local staging copy
            shutil.rmtree(local_ep, ignore_errors=True)
            _cleanup_robot(cfg, uuid, remote_path, step, log,
                           server_files=server_files)
            return {"import_id": import_id, "dataset_id": st.get("dataset_id"),
                    "n_frames": n_frames, "remote_path": remote_path}
        if st["status"] == "error":
            raise RuntimeError(f"변환 실패: {st}")
        prog = st.get("progress")  # free-form text (e.g. "converting 1 episode(s)")
        log(f"  변환 진행: {str(prog)[:60] or st['status']} ...")
        time.sleep(5)
    raise TimeoutError("변환 대기 시간 초과")


def run_local(cfg: Config, uuid: str, duration_s: int, task: str,
              recorder: str = "", step_cb=None, log=print):
    """서버 없이 노트북에 저장: 회수 → h5 → meta → raw 보관 → LeRobot append.

    산출물은 서버 모드의 변환 결과와 동일 포맷 — 나중에 플랫폼이 생기면
    데이터셋 경로 import 또는 raw HTTP 업로드로 그대로 등록 가능하다.
    """

    def step(i, state):
        if step_cb:
            step_cb(i, state)

    local_ep, n_frames = _pull_and_prepare(
        cfg, uuid, duration_s, task, recorder, cfg.local.dataset, step, log)

    # store_episode가 raw 보관(3)과 append 변환(4)을 함께 수행
    step(3, "run")
    step(4, "run")
    try:
        res = local_store.store_episode(cfg, local_ep, uuid, task, log=log)
    except Exception:
        step(4, "fail")
        raise
    step(3, "ok")
    step(4, "ok")
    ep_dir = res["ep_dir"]

    step(5, "run")
    if res["n_frames"] != n_frames:
        raise RuntimeError(f"데이터셋 검증 실패: h5 {n_frames}프레임 vs "
                           f"변환본 {res['n_frames']}프레임")
    log(f"데이터셋 '{cfg.local.dataset}' — 누적 {res['n_episodes']}개 에피소드")
    step(5, "ok")

    _cleanup_robot(cfg, uuid, ep_dir, step, log,
                   server_files=local_store.local_manifest(ep_dir))
    if not cfg.local.keep_raw:
        shutil.rmtree(local_ep, ignore_errors=True)
    return {"dataset": res["dataset"], "n_frames": n_frames,
            "n_episodes": res["n_episodes"]}


def _cleanup_robot(cfg: Config, uuid: str, remote_path: str, step, log,
                   server_files: dict | None = None):
    """Delete the robot original once every robot file is size-verified on the
    server. Non-fatal: the episode is already imported, so on any mismatch or
    error we keep the robot copy, warn, and let the job finish as done.

    server_files: http 업로드 응답의 {relpath: size} manifest. 없으면(sftp 모드)
    SSH로 서버 사본을 직접 조회한다."""
    if not cfg.recording.cleanup_robot:
        return
    step(6, "run")
    try:
        robot = RobotLink(cfg.robot)
        try:
            robot_files = robot.manifest(uuid)
            if server_files is None:
                server = ServerLink(cfg.server)
                try:
                    server_files = server.manifest(remote_path)
                finally:
                    server.close()
            bad = [p for p, size in robot_files.items() if server_files.get(p) != size]
            if bad:
                raise RuntimeError(f"서버 사본 불일치 {len(bad)}건 (예: {bad[:3]})")
            robot.delete_episode(uuid)
        finally:
            robot.close()
        log(f"로봇 원본 삭제 완료 ({len(robot_files)}개 파일 사본 검증 통과)")
        step(6, "ok")
    except Exception as e:
        # raw + converted copies are on the server; the robot copy just lingers
        log(f"경고: 로봇 원본 정리 실패 — 원본 보존됨. 사유: {e}")
        step(6, "fail")

"""Background upload queue: episodes recorded → pipeline runs one at a time.

State persists to <staging_dir>/upload_queue.json so unfinished jobs resume
after a GUI crash/restart. All pipeline work happens in a single worker thread;
UI observes via the jobs_changed signal.
"""
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from . import pipeline
from .config import Config

STEP_NAMES = pipeline.STEPS


@dataclass
class UploadJob:
    uuid: str
    project_id: int                   # local 모드에서는 0
    project_name: str                 # local 모드에서는 "(로컬)"
    job_id: int                       # local 모드에서는 0
    job_name: str                     # local 모드에서는 데이터셋 이름
    description: str                  # local 모드에서는 task 텍스트
    duration_s: int
    created_at: str
    mode: str = "server"              # server | local (pipeline 선택)
    status: str = "queued"            # queued | running | done | error
    current_step: int = -1
    step_states: list = field(default_factory=lambda: ["idle"] * len(STEP_NAMES))
    error: str = ""
    dataset_id: int | None = None
    n_frames: int | None = None


class UploadManager(QObject):
    jobs_changed = pyqtSignal()
    log = pyqtSignal(str)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.path = Path(cfg.staging_dir).expanduser() / "upload_queue.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs: list[UploadJob] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._load()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # ---------------------------------------------------------- persistence
    def _load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                self.jobs = [UploadJob(**j) for j in raw]
                for j in self.jobs:
                    if j.status == "running":   # crashed mid-run -> retry
                        j.status = "queued"
                        j.step_states = ["idle"] * len(STEP_NAMES)
                        j.current_step = -1
                    elif len(j.step_states) < len(STEP_NAMES):  # queue from older STEPS
                        j.step_states += ["idle"] * (len(STEP_NAMES) - len(j.step_states))
            except Exception:
                self.jobs = []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(j) for j in self.jobs], ensure_ascii=False, indent=1))
        tmp.replace(self.path)

    # ----------------------------------------------------------------- API
    def enqueue(self, **kwargs):
        job = UploadJob(created_at=time.strftime("%m-%d %H:%M:%S"), **kwargs)
        with self._lock:
            self.jobs.append(job)
            self._save()
        self.jobs_changed.emit()
        self._wake.set()
        return job

    def retry(self, uuid):
        with self._lock:
            for j in self.jobs:
                if j.uuid == uuid and j.status == "error":
                    j.status = "queued"
                    j.error = ""
                    j.step_states = ["idle"] * len(STEP_NAMES)
                    j.current_step = -1
            self._save()
        self.jobs_changed.emit()
        self._wake.set()

    def clear_done(self):
        with self._lock:
            self.jobs = [j for j in self.jobs if j.status != "done"]
            self._save()
        self.jobs_changed.emit()

    def pending_count(self):
        with self._lock:
            return sum(1 for j in self.jobs if j.status in ("queued", "running"))

    # -------------------------------------------------------------- worker
    def _next_job(self):
        with self._lock:
            for j in self.jobs:
                if j.status == "queued":
                    j.status = "running"
                    self._save()
                    return j
        return None

    def _worker(self):
        while True:
            job = self._next_job()
            if job is None:
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue
            self.jobs_changed.emit()

            def step_cb(i, state):
                job.current_step = i
                job.step_states[i] = state
                with self._lock:
                    self._save()
                self.jobs_changed.emit()

            try:
                _log = lambda m: self.log.emit(f"[{job.uuid[:8]}] {m}")  # noqa: E731
                if job.mode == "local":
                    res = pipeline.run_local(
                        self.cfg, job.uuid, job.duration_s, job.description,
                        step_cb=step_cb, log=_log)
                else:
                    res = pipeline.run(
                        self.cfg, job.uuid, job.project_id, job.job_id,
                        job.duration_s, job.description, job_name=job.job_name,
                        step_cb=step_cb, log=_log)
                job.status = "done"
                job.dataset_id = res.get("dataset_id")
                job.n_frames = res.get("n_frames")
            except Exception as e:
                job.status = "error"
                job.error = str(e)[:300]
                if 0 <= job.current_step < len(STEP_NAMES):
                    job.step_states[job.current_step] = "fail"
                self.log.emit(f"[{job.uuid[:8]}] 실패: {e}")
            with self._lock:
                self._save()
            self.jobs_changed.emit()

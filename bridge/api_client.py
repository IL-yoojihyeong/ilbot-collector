"""IL-BOT Data Studio(구 RoboLabel) REST API client."""
import os
import tarfile
import tempfile

import requests

from .config import ServerCfg


class _ProgressFile:
    """read()마다 진행 콜백을 부르는 파일 래퍼.

    requests는 read()로 스트리밍 전송하고 __len__으로 Content-Length를 정한다.
    """

    def __init__(self, f, total, progress):
        self._f = f
        self._total = total
        self._sent = 0
        self._progress = progress

    def __len__(self):
        return self._total

    def read(self, size=-1):
        chunk = self._f.read(size)
        if chunk and self._progress:
            self._sent += len(chunk)
            self._progress(self._sent, self._total)
        return chunk


class RoboLabelAPI:
    def __init__(self, cfg: ServerCfg, timeout: float = 10.0):
        self.base = cfg.api_url.rstrip("/")
        self.timeout = timeout
        self.auth = (cfg.api_user, cfg.api_password) if cfg.api_password else None

    def _get(self, path, **kw):
        r = requests.get(self.base + path, auth=self.auth, timeout=self.timeout, **kw)
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        r = requests.post(self.base + path, json=body, auth=self.auth, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------- checks
    def ping(self) -> bool:
        try:
            self._get("/api/projects")
            return True
        except Exception:
            return False

    # -------------------------------------------------------------- reads
    def projects(self):
        return self._get("/api/projects")

    def jobs(self, project_id: int):
        return self._get(f"/api/projects/{project_id}/jobs")

    def imports(self, project_id: int):
        return self._get(f"/api/projects/{project_id}/imports")

    def episodes(self, project_id: int):
        return self._get(f"/api/projects/{project_id}/episodes")

    # ------------------------------------------------------------- writes
    def create_import(self, project_id: int, server_path: str, job_id: int):
        """Register a server-side episode path for background conversion."""
        return self._post(f"/api/projects/{project_id}/imports",
                          {"path": server_path, "job_id": job_id})

    def upload_episode(self, job_id: int, uuid: str, ep_dir: str, progress=None):
        """에피소드 폴더를 tar로 묶어 HTTP 업로드 (transport=http 경로).

        서버가 raw/<uuid>에 풀고 import까지 시작한다. 반환:
        {import_id, project_id, format, files: {relpath: size}}.
        tar는 스테이징 옆 임시 파일로 만들어 스트리밍 전송(메모리 상주 없음).
        """
        ep_dir = str(ep_dir)
        with tempfile.NamedTemporaryFile(
                dir=os.path.dirname(ep_dir) or ".", suffix=".upload.tar") as tmp:
            with tarfile.open(tmp.name, "w") as tar:   # 무압축 — h265/pbdat는 압축 이득 없음
                for root, _, fs in os.walk(ep_dir):
                    for fn in sorted(fs):
                        full = os.path.join(root, fn)
                        tar.add(full, arcname=os.path.relpath(full, ep_dir))
            total = os.path.getsize(tmp.name)
            with open(tmp.name, "rb") as f:
                r = requests.post(
                    f"{self.base}/api/jobs/{job_id}/episodes",
                    params={"uuid": uuid},
                    data=_ProgressFile(f, total, progress),
                    headers={"Content-Type": "application/x-tar"},
                    auth=self.auth, timeout=(10, 1800))
        r.raise_for_status()
        return r.json()

    def import_status(self, project_id: int, import_id: int):
        for t in self.imports(project_id):
            if t["id"] == import_id:
                return t
        return None

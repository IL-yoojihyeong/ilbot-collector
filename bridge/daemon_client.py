"""HTTP client for the robot-side bridge daemon + lifecycle management."""
import time

import requests

from .config import RobotCfg, REPO_ROOT
from .ssh_link import SSHSession

DAEMON_FILES = ["bridge_daemon.py", "camera_streamer.py", "start_daemon.sh"]


class DaemonClient:
    def __init__(self, cfg: RobotCfg, port: int = 18800):
        self.cfg = cfg
        self.base = f"http://{cfg.ssh_host}:{port}"

    # ------------------------------------------------------------- HTTP api
    def status(self, timeout=3.0):
        try:
            r = requests.get(self.base + "/status", timeout=timeout)
            return r.json()
        except Exception:
            return None

    def heartbeat(self):
        try:
            requests.post(self.base + "/heartbeat", json={}, timeout=3.0)
            return True
        except Exception:
            return False

    def start(self, uuid, post_win_ms=600000, cameras=None):
        body = {"uuid": uuid, "post_win_ms": post_win_ms}
        if cameras:
            body["cameras"] = cameras
        r = requests.post(self.base + "/start", json=body, timeout=15.0)
        return r.json()

    def stop(self, uuid, post_win_ms=600000):
        r = requests.post(self.base + "/stop",
                          json={"uuid": uuid, "post_win_ms": post_win_ms}, timeout=15.0)
        return r.json()

    def shutdown(self, force=False):
        try:
            r = requests.post(self.base + "/shutdown", json={"force": force}, timeout=5.0)
            return r.json()
        except Exception:
            return None

    def get_frame(self, cam=None, timeout=3.0):
        url = self.base + "/frame.jpg" + (f"?cam={cam}" if cam else "")
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
        return None

    # ------------------------------------------------------------ lifecycle
    def ensure_running(self, stream_cams=None, stream_fps=4.0,
                       idle_timeout=900, log=print):
        """Deploy daemon files and (re)start via start_daemon.sh if not healthy."""
        stream_cams = stream_cams or ["head_color"]
        st = self.status()
        if st and st.get("ok"):
            log("데몬이 이미 실행 중 — 재사용")
            return st
        log("데몬 배포·시작 중...")
        ssh = SSHSession(self.cfg.ssh_host, self.cfg.ssh_user, self.cfg.ssh_password)
        start_out = ""
        try:
            ssh.run(f"mkdir -p {self.cfg.ctl_dir}")
            # 데몬(agi)이 에피소드 dir을 선생성할 수 있게 record_root에 sticky 쓰기
            # 권한 부여 (GDK 2.3.4 dds_record는 storage_folder를 스스로 안 만듦)
            ssh.run_sudo(f"chmod 1777 {self.cfg.record_root}", self.cfg.ssh_password)
            for fn in DAEMON_FILES:
                ssh.sftp.put(str(REPO_ROOT / "robot" / fn), f"{self.cfg.ctl_dir}/{fn}")
            rc, out, err = ssh.run(
                f"bash {self.cfg.ctl_dir}/start_daemon.sh --stream-cams {','.join(stream_cams)} "
                f"--stream-fps {stream_fps} --idle-timeout {idle_timeout}", timeout=40)
            start_out = (out + err).strip()
            log(start_out.splitlines()[-1] if start_out else f"start rc={rc}")
        finally:
            ssh.close()
        for _ in range(20):
            st = self.status()
            if st and st.get("ok"):
                return st
            time.sleep(1)
        raise RuntimeError(f"데몬 시작 실패: {start_out[-400:]}")

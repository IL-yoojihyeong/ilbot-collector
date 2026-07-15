"""SSH/SFTP links to the robot and the platform server (paramiko, password auth)."""
import os
import posixpath
import re
import shlex
import stat
from pathlib import Path

import paramiko

from .config import RobotCfg, ServerCfg, H5_INPUT_PBDATS


class SSHSession:
    def __init__(self, host, user, password, timeout=10):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(host, username=user, password=password,
                            timeout=timeout, allow_agent=False, look_for_keys=False)
        self._sftp = None

    @property
    def sftp(self):
        if self._sftp is None:
            self._sftp = self.client.open_sftp()
        return self._sftp

    def run(self, cmd, timeout=60):
        _, out, err = self.client.exec_command(cmd, timeout=timeout)
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode(errors="replace"), err.read().decode(errors="replace")

    def run_sudo(self, cmd, password, timeout=60):
        """Run cmd as root (password fed to sudo via stdin)."""
        return self.run(f"echo {shlex.quote(password)} | sudo -S -p '' {cmd}", timeout=timeout)

    def get_tree(self, remote_dir, local_dir, name_filter=None, progress=None):
        """Recursively download remote_dir into local_dir."""
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        for entry in self.sftp.listdir_attr(remote_dir):
            rpath = posixpath.join(remote_dir, entry.filename)
            lpath = os.path.join(local_dir, entry.filename)
            if stat.S_ISDIR(entry.st_mode):
                self.get_tree(rpath, lpath, name_filter, progress)
            else:
                if name_filter and not name_filter(rpath):
                    continue
                if progress:
                    progress(rpath, entry.st_size)
                self.sftp.get(rpath, lpath)

    def manifest(self, remote_dir):
        """{relative_path: size} for every regular file under remote_dir."""
        rc, out, err = self.run(f"find '{remote_dir}' -type f -printf '%P\\t%s\\n'")
        if rc != 0:
            raise RuntimeError(f"manifest failed for {remote_dir}: {err.strip()[:200]}")
        files = {}
        for line in out.splitlines():
            path, _, size = line.rpartition("\t")
            if path:
                files[path] = int(size)
        return files

    def put_tree(self, local_dir, remote_dir, progress=None):
        self.run(f"mkdir -p '{remote_dir}'")
        for root, dirs, files in os.walk(local_dir):
            rel = os.path.relpath(root, local_dir)
            rdir = remote_dir if rel == "." else posixpath.join(remote_dir, rel.replace(os.sep, "/"))
            if rel != ".":
                self.run(f"mkdir -p '{rdir}'")
            for fn in files:
                lpath = os.path.join(root, fn)
                if progress:
                    progress(lpath, os.path.getsize(lpath))
                self.sftp.put(lpath, posixpath.join(rdir, fn))

    def close(self):
        try:
            if self._sftp:
                self._sftp.close()
            self.client.close()
        except Exception:
            pass


class RobotLink:
    """Recording control + episode retrieval on the G2 robot."""

    def __init__(self, cfg: RobotCfg):
        self.cfg = cfg
        self.ssh = SSHSession(cfg.ssh_host, cfg.ssh_user, cfg.ssh_password)

    # 녹화 제어는 상주 데몬(robot/bridge_daemon.py + daemon_client) 경유가 유일 경로.
    # 단발 CLI(record_ctl) 방식은 robot/legacy/로 이동 (2026-07-15 정리).

    def episode_exists(self, uuid):
        rc, _, _ = self.ssh.run(f"test -d {self.cfg.record_root}/{uuid}")
        return rc == 0

    def manifest(self, uuid):
        return self.ssh.manifest(f"{self.cfg.record_root}/{uuid}")

    def delete_episode(self, uuid):
        """rm -rf the episode dir (sudo — dds_record writes files as root).
        uuid format is validated so a malformed value can never widen the path."""
        if not re.fullmatch(r"[0-9a-fA-F][0-9a-fA-F-]{7,}", uuid):
            raise ValueError(f"suspicious uuid, refusing to delete: {uuid!r}")
        path = f"{self.cfg.record_root}/{uuid}"
        rc, _, err = self.ssh.run_sudo(f"rm -rf '{path}'", self.cfg.ssh_password, timeout=300)
        if rc != 0:
            raise RuntimeError(f"rm failed: {err.strip()[:200]}")
        if self.episode_exists(uuid):
            raise RuntimeError(f"episode dir still present after rm: {path}")

    def pull_episode(self, uuid, staging_dir, pull_all_pbdat=False, progress=None):
        """Download camera/ + (needed) pbdats into <staging>/<uuid>. Returns local path."""
        remote = f"{self.cfg.record_root}/{uuid}"
        local = os.path.join(os.path.expanduser(staging_dir), uuid)

        def pbdat_filter(rpath):
            name = posixpath.basename(rpath)
            if not name.endswith(".pbdat"):
                return True
            return pull_all_pbdat or name in H5_INPUT_PBDATS

        self.ssh.get_tree(remote + "/camera", os.path.join(local, "camera"), progress=progress)
        self.ssh.get_tree(remote + "/record", os.path.join(local, "record"),
                          name_filter=pbdat_filter, progress=progress)
        return local

    def close(self):
        self.ssh.close()


class ServerLink:
    """Episode upload to the platform server."""

    def __init__(self, cfg: ServerCfg):
        self.cfg = cfg
        self.ssh = SSHSession(cfg.ssh_host, cfg.ssh_user, cfg.ssh_password)

    def push_episode(self, local_ep_dir, uuid, push_pbdat=False, progress=None):
        remote = posixpath.join(self.cfg.incoming_dir, uuid)
        if push_pbdat:
            self.ssh.put_tree(local_ep_dir, remote, progress=progress)
        else:
            # h5 + camera + meta only — that's all the converter needs
            self.ssh.run(f"mkdir -p '{remote}/record'")
            self.ssh.sftp.put(os.path.join(local_ep_dir, "meta_info.json"),
                              posixpath.join(remote, "meta_info.json"))
            self.ssh.sftp.put(os.path.join(local_ep_dir, "record", "aligned_joints.h5"),
                              posixpath.join(remote, "record", "aligned_joints.h5"))
            self.ssh.put_tree(os.path.join(local_ep_dir, "camera"),
                              posixpath.join(remote, "camera"), progress=progress)
        return remote

    def manifest(self, remote_path):
        return self.ssh.manifest(remote_path)

    def close(self):
        self.ssh.close()

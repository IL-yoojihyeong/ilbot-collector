#!/usr/bin/env python3
"""RoboLabel bridge daemon — runs ON the robot, deployed by the GUI.

Keeps DDS publishers/subscribers warm so record start/stop is instant, serves a
low-fps camera stream, and self-terminates when the GUI stops sending
heartbeats (crash safety) — unless a recording is in progress.

HTTP API (default :18800):
  GET  /status       {ok, ptp_ready, recording, uptime_s, heartbeat_age_s, stream_cam}
  POST /heartbeat    keep-alive from the GUI
  POST /start        {"uuid": ..., "post_win_ms"?, "cameras"?, "topics"?}
  POST /stop         {"uuid": ...}
  GET  /frame.jpg    latest camera frame (JPEG)
  GET  /stream.mjpg  multipart MJPEG stream
  POST /shutdown     graceful exit (409 while recording unless {"force": true})
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, '/home/agi/app/gdk/lib')
sys.path.insert(0, '/home/agi/app/local/lib/python3.10/dist-packages')
os.environ.setdefault("LOCATOR_IP", "10.42.1.101")
os.environ.setdefault("AORTA_DISCOVERY_URI", "http://10.42.1.101:2379")

from agibot_gdk.dds import Node, GDKQoS  # noqa: E402
from dlb_msgs_pb import dlb_record_pb2 as rec  # noqa: E402
from genie_msgs_pb.msg.JointState_pb2 import JointState  # noqa: E402

RECORD_ROOT = "/data/record"
TOPIC_CONF = "/home/agi/app/conf/record/record_topic.json"
DEFAULT_CAMERAS = ["head_color", "hand_left_color", "hand_right_color"]
FRAME_DIR = "/dev/shm/robolabel_bridge"
# NOTE: agibot_gdk.Camera() must NOT be created in this process — it deadlocks
# alongside a GDK DDS Node. Streaming runs in camera_streamer.py (child process).


def load_all_topics():
    with open(TOPIC_CONF) as f:
        return [e["name"] for e in json.load(f)]


class Daemon:
    def __init__(self, args):
        self.args = args
        self.t0 = time.time()
        self.last_heartbeat = time.time()
        self.lock = threading.Lock()
        self.recording = None          # uuid | None
        self.rec_started_at = None
        self.responses = {}            # uuid -> {src: {...}}
        self.ptp_offset_ns = None
        self.all_topics = load_all_topics()
        self.stop_flag = False
        self.streamer = None

        self.node = Node("robolabel_bridge_daemon")
        qos = GDKQoS()
        self.node.create_subscriber("/hal/joint_state", JointState, qos, self._on_joint)
        self.node.create_subscriber("/dlb_msgs/record_dds_response", rec.RecordResponse,
                                    qos, self._on_resp("dds"))
        self.node.create_subscriber("/dlb_msgs/record_video_response", rec.RecordResponse,
                                    qos, self._on_resp("video"))
        self.pub_dds = self.node.create_publisher("/dlb_msgs/record_dds_request",
                                                  rec.RecordRequest, qos)
        self.pub_vid = self.node.create_publisher("/dlb_msgs/record_video_request",
                                                  rec.RecordRequest, qos)

        self._spawn_streamer()
        threading.Thread(target=self._watchdog_loop, daemon=True).start()

    # ------------------------------------------------------------------ DDS
    def _on_joint(self, msg, info):
        if info.publish_ts_ns > 0:
            self.ptp_offset_ns = info.publish_ts_ns - time.time_ns()

    def _on_resp(self, src):
        def cb(msg, info):
            with self.lock:
                self.responses.setdefault(msg.req_uuid, {})[src] = {
                    "result": int(msg.result), "err": msg.err_msg,
                    "fails": [{"key": f.key, "err": f.err_msg} for f in msg.fail_infos],
                }
        return cb

    def _make_req(self, uuid, command, props, post_win_ms):
        r = rec.RecordRequest()
        r.uuid = uuid
        r.command = command
        r.timestamp_utc = int(time.time())
        r.timestamp_ptp = time.time_ns() + (self.ptp_offset_ns or 0)
        for key, folder in props:
            p = r.record_props.add()
            p.key = key
            p.storage_folder = folder
            p.pre_win = 0
            p.post_win = post_win_ms
        return r

    def start_record(self, uuid, post_win_ms, cameras, topics):
        if self.ptp_offset_ns is None:
            return {"ok": False, "error": "ptp not ready"}
        with self.lock:
            if self.recording:
                return {"ok": False, "error": f"already recording {self.recording}"}
            self.responses.pop(uuid, None)
        base = f"{RECORD_ROOT}/{uuid}"
        topics = topics or self.all_topics
        cameras = cameras or self.args.cameras
        # GDK 2.3.4의 dds_record는 storage_folder 디렉토리를 스스로 만들지 않아
        # 40토픽 전부 'create file failed'가 난다(2.6.3은 만들어 줌) → 선생성.
        # /data/record 쓰기 권한은 배포 시 daemon_client가 chmod 1777로 확보.
        try:
            os.makedirs(base + "/record", exist_ok=True)
            for c in cameras:
                os.makedirs(f"{base}/camera/{c}", exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"episode dir 생성 실패: {e} "
                                          f"(record_root 권한 확인 — chmod 1777)"}
        self.pub_dds.publish(self._make_req(
            uuid, rec.RecordRequest.START, [(t, base + "/record") for t in topics], post_win_ms))
        self.pub_vid.publish(self._make_req(
            uuid, rec.RecordRequest.START, [(c, f"{base}/camera/{c}") for c in cameras], post_win_ms))

        # 성공 판정은 h5에 필수인 joint_state pbdat이 실제로 생기는 것 기준
        # (dir는 위에서 선생성했으므로 신호가 못 됨; 응답은 지연·유실 가능).
        # result=0이면 성공, result=2(부분 실패)는 joint_state가 실패 목록에
        # 없을 때만 파일 대기 계속.
        joint_file = base + "/record/-hal-joint_state.pbdat"
        t0 = time.time()
        ok, verified, got = False, None, {}
        while time.time() - t0 < 5.0:
            if os.path.isfile(joint_file):
                ok, verified = True, "joint_pbdat"
                break
            with self.lock:
                got = dict(self.responses.get(uuid, {}))
            dds = got.get("dds", {})
            if dds.get("result") == 0:
                ok, verified = True, "response"
                break
            if dds.get("result") == 2 and any(
                    f["key"] == "/hal/joint_state" for f in dds.get("fails", [])):
                break                      # 핵심 토픽 실패 — 즉시 실패 처리
            time.sleep(0.05)
        with self.lock:
            got = dict(self.responses.get(uuid, {}))
        if ok:
            with self.lock:
                self.recording = uuid
                self.rec_started_at = time.time()
        return {"ok": ok, "uuid": uuid, "base": base, "verified": verified,
                "responses": got, "n_topics": len(topics), "cameras": cameras}

    def stop_record(self, uuid, post_win_ms, cameras, topics):
        base = f"{RECORD_ROOT}/{uuid}"
        topics = topics or self.all_topics
        cameras = cameras or self.args.cameras
        with self.lock:
            self.responses.pop(uuid, None)
        self.pub_dds.publish(self._make_req(
            uuid, rec.RecordRequest.STOP, [(t, base + "/record") for t in topics], post_win_ms))
        self.pub_vid.publish(self._make_req(
            uuid, rec.RecordRequest.STOP, [(c, f"{base}/camera/{c}") for c in cameras], post_win_ms))
        t0 = time.time()
        while time.time() - t0 < 4.0:
            with self.lock:
                got = self.responses.get(uuid, {})
            if len(got) >= 2:
                break
            time.sleep(0.05)
        with self.lock:
            if self.recording == uuid:
                self.recording = None
                self.rec_started_at = None
        return {"ok": True, "uuid": uuid, "responses": got}

    # --------------------------------------------------------------- camera
    def _spawn_streamer(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_streamer.py")
        if not os.path.exists(script):
            print("camera_streamer.py missing — stream disabled", flush=True)
            return
        cmd = (f"set --; source /home/agi/app/env.sh >/dev/null 2>&1; "
               f"exec python3 {script} --cams {','.join(self.args.stream_cams)} "
               f"--fps {self.args.stream_fps} --outdir {FRAME_DIR}")
        self.streamer = subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=open("/tmp/robolabel_bridge/streamer.log", "ab"),
            stderr=subprocess.STDOUT)
        print(f"camera streamer pid {self.streamer.pid}", flush=True)

    def latest_jpeg(self, cam=None):
        cam = cam or self.args.stream_cams[0]
        path = os.path.join(FRAME_DIR, f"frame_{cam}.jpg")
        try:
            with open(path, "rb") as f:
                return f.read(), os.path.getmtime(path)
        except OSError:
            return None, 0

    # ------------------------------------------------------------- watchdog
    def _watchdog_loop(self):
        while not self.stop_flag:
            time.sleep(5)
            if self.streamer and self.streamer.poll() is not None:
                print("streamer died — restarting", flush=True)
                self._spawn_streamer()
            age = time.time() - self.last_heartbeat
            with self.lock:
                busy = self.recording is not None
            if age > self.args.idle_timeout and not busy:
                print(f"no heartbeat for {int(age)}s and idle — exiting", flush=True)
                self.cleanup()
                os._exit(0)

    def cleanup(self):
        if self.streamer and self.streamer.poll() is None:
            self.streamer.terminate()

    def status(self):
        with self.lock:
            rec_uuid = self.recording
            rec_elapsed = time.time() - self.rec_started_at if self.rec_started_at else None
        ready = {}
        for c in self.args.stream_cams:
            _, mtime = self.latest_jpeg(c)
            ready[c] = (time.time() - mtime) < 5.0
        return {"ok": True, "ptp_ready": self.ptp_offset_ns is not None,
                "recording": rec_uuid, "recording_elapsed_s": rec_elapsed,
                "uptime_s": round(time.time() - self.t0, 1),
                "heartbeat_age_s": round(time.time() - self.last_heartbeat, 1),
                "stream_cams": self.args.stream_cams, "stream_ready": ready,
                "n_topics": len(self.all_topics)}


def make_handler(d: Daemon):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _query_cam(self):
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("cam="):
                        return kv[4:]
            return None

        def do_GET(self):
            route = self.path.split("?")[0]
            if route == "/status":
                self._json(d.status())
            elif route == "/frame.jpg":
                jpeg, _ = d.latest_jpeg(self._query_cam())
                if not jpeg:
                    self._json({"error": "no frame yet"}, 503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.end_headers()
                self.wfile.write(jpeg)
            elif route == "/stream.mjpg":
                self.send_response(200)
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                last_mtime = 0
                cam = self._query_cam()
                try:
                    while True:
                        jpeg, mtime = d.latest_jpeg(cam)
                        if jpeg is None or mtime == last_mtime:
                            time.sleep(0.08)
                            continue
                        last_mtime = mtime
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                         + f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self.path == "/heartbeat":
                d.last_heartbeat = time.time()
                self._json({"ok": True})
            elif self.path == "/start":
                b = self._body()
                d.last_heartbeat = time.time()
                self._json(d.start_record(b["uuid"], b.get("post_win_ms", 600000),
                                          b.get("cameras"), b.get("topics")))
            elif self.path == "/stop":
                b = self._body()
                d.last_heartbeat = time.time()
                self._json(d.stop_record(b["uuid"], b.get("post_win_ms", 600000),
                                         b.get("cameras"), b.get("topics")))
            elif self.path == "/shutdown":
                b = self._body()
                with d.lock:
                    busy = d.recording is not None
                if busy and not b.get("force"):
                    self._json({"ok": False, "error": "recording in progress"}, 409)
                    return
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.3), d.cleanup(), os._exit(0)),
                                 daemon=True).start()
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18800)
    ap.add_argument("--idle-timeout", type=int, default=900)
    ap.add_argument("--stream-cams", default="head_color,hand_left_color,hand_right_color")
    ap.add_argument("--stream-fps", type=float, default=4.0)
    ap.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    args = ap.parse_args()
    args.cameras = [c for c in args.cameras.split(",") if c]
    args.stream_cams = [c for c in args.stream_cams.split(",") if c]

    pid_dir = "/tmp/robolabel_bridge"
    os.makedirs(pid_dir, exist_ok=True)
    with open(f"{pid_dir}/daemon.pid", "w") as f:
        f.write(str(os.getpid()))

    d = Daemon(args)
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(d))
    print(f"bridge daemon on :{args.port} (topics={len(d.all_topics)})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

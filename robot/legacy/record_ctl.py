#!/usr/bin/env python3
"""Stateless record start/stop CLI — runs ON the robot (needs agibot_gdk).

Deployed to /tmp/robolabel_bridge/record_ctl.py by the bridge GUI.
Prints a single JSON line as the last stdout line:
  {"ok": true, "uuid": "...", "responses": {"dds": {...}, "video": {...}}}

Usage:
  record_ctl.py start --uuid U [--topics a,b] [--cameras x,y] [--post-win-ms N]
  record_ctl.py stop  --uuid U [--topics a,b] [--cameras x,y]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, '/home/agi/app/gdk/lib')
sys.path.insert(0, '/home/agi/app/local/lib/python3.10/dist-packages')
os.environ.setdefault("LOCATOR_IP", "10.42.1.101")
os.environ.setdefault("AORTA_DISCOVERY_URI", "http://10.42.1.101:2379")

from agibot_gdk.dds import Node, GDKQoS
from dlb_msgs_pb import dlb_record_pb2 as rec
from genie_msgs_pb.msg.JointState_pb2 import JointState

DEFAULT_TOPICS = ",".join([
    "/hal/joint_state", "/hal/joint_cmd", "/hal/left_ee_data",
    "/hal/right_ee_data", "/hal/usr_state", "/hal/whole_body_status",
    "/hal/chassis_joint_state", "/genie/bundle_data", "/tf",
    "/dr/odom", "/imu/chassis",
    "/remote/vr_data", "/remote/vr_network",
    "/wbc/arm_command", "/wbc/gripper_command",
    "/wbc/left_ee_command", "/wbc/right_ee_command", "/wbc/retarget",
])
DEFAULT_CAMERAS = "head_color,hand_left_color,hand_right_color"
RECORD_ROOT = "/data/record"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["start", "stop"])
    ap.add_argument("--uuid", required=True)
    ap.add_argument("--topics", default=DEFAULT_TOPICS)
    ap.add_argument("--cameras", default=DEFAULT_CAMERAS)
    ap.add_argument("--post-win-ms", type=int, default=600000)
    ap.add_argument("--resp-wait", type=float, default=6.0)
    ap.add_argument("--warmup", type=float, default=3.5,
                    help="min seconds between node creation and publish (discovery)")
    args = ap.parse_args()

    topics = [t for t in args.topics.split(",") if t]
    cameras = [c for c in args.cameras.split(",") if c]
    base = os.path.join(RECORD_ROOT, args.uuid)

    node = Node("robolabel_record_ctl")
    ptp = {"offset_ns": None}
    responses = {}

    def on_joint(msg, info):
        if ptp["offset_ns"] is None and info.publish_ts_ns > 0:
            ptp["offset_ns"] = info.publish_ts_ns - time.time_ns()

    def on_resp(src):
        def cb(msg, info):
            if msg.req_uuid == args.uuid:
                responses[src] = {
                    "result": int(msg.result),
                    "err": msg.err_msg,
                    "fails": [{"key": f.key, "err": f.err_msg} for f in msg.fail_infos],
                }
        return cb

    qos = GDKQoS()
    node.create_subscriber("/hal/joint_state", JointState, qos, on_joint)
    node.create_subscriber("/dlb_msgs/record_dds_response", rec.RecordResponse, qos, on_resp("dds"))
    node.create_subscriber("/dlb_msgs/record_video_response", rec.RecordResponse, qos, on_resp("video"))
    pub_dds = node.create_publisher("/dlb_msgs/record_dds_request", rec.RecordRequest, qos)
    pub_vid = node.create_publisher("/dlb_msgs/record_video_request", rec.RecordRequest, qos)

    node_t0 = time.time()
    for _ in range(100):  # wait for PTP reference (max 10s)
        if ptp["offset_ns"] is not None:
            break
        time.sleep(0.1)
    if ptp["offset_ns"] is None:
        print(json.dumps({"ok": False, "error": "no ptp reference (is /hal/joint_state alive?)"}))
        sys.exit(1)
    # response subscribers / request publishers need discovery time; the PTP
    # reference can arrive fast, so enforce a minimum warmup since node creation
    remain = args.warmup - (time.time() - node_t0)
    if remain > 0:
        time.sleep(remain)

    def make_req(command, props):
        r = rec.RecordRequest()
        r.uuid = args.uuid
        r.command = command
        r.timestamp_utc = int(time.time())
        r.timestamp_ptp = time.time_ns() + ptp["offset_ns"]
        for key, folder in props:
            p = r.record_props.add()
            p.key = key
            p.storage_folder = folder
            p.pre_win = 0
            p.post_win = args.post_win_ms
        return r

    cmd = rec.RecordRequest.START if args.command == "start" else rec.RecordRequest.STOP
    pub_dds.publish(make_req(cmd, [(t, base + "/record") for t in topics]))
    pub_vid.publish(make_req(cmd, [(c, base + "/camera/" + c) for c in cameras]))

    t0 = time.time()
    while time.time() - t0 < args.resp_wait:
        if len(responses) >= 2:
            break
        time.sleep(0.1)

    ok = all(r["result"] in (0, 2) for r in responses.values())
    verified = "response"
    if args.command == "start" and "dds" not in responses:
        # response can be lost to a discovery race — the recording may still have
        # started; the episode dir appearing is authoritative
        ok = False
        for _ in range(30):
            if os.path.isdir(os.path.join(base, "record")):
                ok = True
                verified = "episode_dir"
                break
            time.sleep(0.1)
    print(json.dumps({"ok": ok, "uuid": args.uuid, "command": args.command,
                      "base": base, "verified": verified, "responses": responses}))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

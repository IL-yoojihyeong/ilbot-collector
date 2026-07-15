#!/usr/bin/env python3
"""RoboLabel teleop record test v5 — 30s real teleop recording with VR/WBC topics."""
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

TEST_UUID = "3f2b8c41-9d7e-4b1a-a6c5-100000000005"
BASE = "/data/record/" + TEST_UUID
TOPICS = ["/hal/joint_state", "/hal/joint_cmd", "/hal/left_ee_data",
          "/hal/right_ee_data", "/hal/usr_state", "/hal/whole_body_status",
          "/hal/chassis_joint_state", "/genie/bundle_data", "/tf",
          "/dr/odom", "/imu/chassis",
          "/remote/vr_data", "/remote/vr_network",
          "/wbc/arm_command", "/wbc/gripper_command",
          "/wbc/left_ee_command", "/wbc/right_ee_command", "/wbc/retarget"]
CAMERAS = ["head_color", "hand_left_color", "hand_right_color"]
RECORD_SECONDS = 30
POST_WIN = 600000  # ms = 10 min window, STOP truncates

node = Node("robolabel_record_test")
responses = []
ptp_state = {"offset_ns": None}


def on_joint(msg, info):
    if ptp_state["offset_ns"] is None and info.publish_ts_ns > 0:
        ptp_state["offset_ns"] = info.publish_ts_ns - time.time_ns()


def on_resp(src):
    def cb(msg, info):
        fails = [(f.key, f.err_msg) for f in msg.fail_infos]
        print("[RESP %s] result=%s err=%r fails=%s"
              % (src, msg.result, msg.err_msg, fails), flush=True)
        responses.append((src, msg))
    return cb


qos = GDKQoS()
node.create_subscriber("/hal/joint_state", JointState, qos, on_joint)
node.create_subscriber("/dlb_msgs/record_dds_response", rec.RecordResponse, qos, on_resp("dds"))
node.create_subscriber("/dlb_msgs/record_video_response", rec.RecordResponse, qos, on_resp("video"))
pub_dds = node.create_publisher("/dlb_msgs/record_dds_request", rec.RecordRequest, qos)
pub_vid = node.create_publisher("/dlb_msgs/record_video_request", rec.RecordRequest, qos)

print("waiting for discovery + ptp ref...", flush=True)
for _ in range(100):
    if ptp_state["offset_ns"] is not None:
        break
    time.sleep(0.1)
if ptp_state["offset_ns"] is None:
    print("!! no ptp reference; abort", flush=True)
    sys.exit(1)
time.sleep(1.0)


def ptp_now_ns():
    return time.time_ns() + ptp_state["offset_ns"]


def make_req(command, props):
    r = rec.RecordRequest()
    r.uuid = TEST_UUID
    r.command = command
    r.timestamp_utc = int(time.time())
    r.timestamp_ptp = ptp_now_ns()
    for key, folder in props:
        p = r.record_props.add()
        p.key = key
        p.storage_folder = folder
        p.pre_win = 0
        p.post_win = POST_WIN
    return r


dds_props = [(t, BASE + "/record") for t in TOPICS]
vid_props = [(c, BASE + "/camera/" + c) for c in CAMERAS]

print("=== START uuid=%s ptp=%d" % (TEST_UUID, ptp_now_ns()), flush=True)
pub_dds.publish(make_req(rec.RecordRequest.START, dds_props))
pub_vid.publish(make_req(rec.RecordRequest.START, vid_props))

for i in range(RECORD_SECONDS):
    time.sleep(1)
    if (i + 1) % 10 == 0:
        print("... recording %ds" % (i + 1), flush=True)

print("=== STOP ptp=%d" % ptp_now_ns(), flush=True)
pub_dds.publish(make_req(rec.RecordRequest.STOP, dds_props))
pub_vid.publish(make_req(rec.RecordRequest.STOP, vid_props))

time.sleep(6.0)
print("=== done, %d responses" % len(responses), flush=True)

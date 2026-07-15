#!/usr/bin/env python3
"""Validate a generated aligned_joints.h5 by loading it with RoboLabel's real G2Episode reader."""
import sys

sys.path.insert(0, '/tmp')
sys.path.insert(0, '/home/agi/app/local/lib/python3.10/dist-packages')

import numpy as np
from agibot import G2Episode

ep = G2Episode(sys.argv[1])
print("n_frames:", ep.n_frames, " fps:", ep.fps)
print("cameras:", ep.available_cameras)
print("state:", ep.state.shape, " action:", ep.action.shape)
print("joint_names:", ep.joint_names[:4], "...", ep.joint_names[-6:])
print("state range: [%.3f, %.3f]  action range: [%.3f, %.3f]"
      % (ep.state.min(), ep.state.max(), ep.action.min(), ep.action.max()))
print("state-action mean |diff|: %.4f rad" % np.abs(ep.state - ep.action).mean())
grip = ep.state[:, 14:16]
print("gripper state cols unique:", np.unique(grip))
for c in ep.available_cameras:
    idx = ep.aligned_frame_indices(c)
    rep = int(np.sum(np.diff(idx) == 0))
    skip = int(np.sum(np.diff(idx) > 1))
    print("%-18s aligned idx [%d..%d] repeats=%d skips=%d" % (c, idx[0], idx[-1], rep, skip))
dt = np.diff(ep.timestamps_ns.astype(np.int64)) / 1e6
print("main_ts dt: mean=%.2fms min=%.2f max=%.2f" % (dt.mean(), dt.min(), dt.max()))
print("VALIDATION PASSED")

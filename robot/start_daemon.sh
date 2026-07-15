#!/bin/bash
# start bridge_daemon detached; safe to re-run (kills previous instance)
cd /tmp/robolabel_bridge || exit 1
pkill -f 'bridge_daemon.py' 2>/dev/null
pkill -f 'camera_streamer.py' 2>/dev/null
sleep 1

# env.sh는 source 시 호출자의 위치 인자를 물려받아 $1을 설치 경로로 오인한다
# (DIR="$1" → find "--stream-cam/lib" → LD_LIBRARY_PATH 미설정 → ImportError).
# 반드시 인자를 비운 뒤 source할 것.
ARGS=("$@")
set --
source /home/agi/app/env.sh >/dev/null 2>&1

setsid nohup python3 /tmp/robolabel_bridge/bridge_daemon.py "${ARGS[@]}" \
    >> /tmp/robolabel_bridge/daemon.log 2>&1 < /dev/null &
sleep 3
if pgrep -f bridge_daemon.py > /dev/null; then
    echo "DAEMON_RUNNING pid=$(pgrep -f bridge_daemon.py)"
else
    echo "DAEMON_FAILED"
    tail -5 /tmp/robolabel_bridge/daemon.log
fi

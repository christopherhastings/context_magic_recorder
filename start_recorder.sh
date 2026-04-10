#!/bin/bash
# start_recorder.sh — Run under Terminal.app for mic TCC permission.
# Opened at login by launchd via osascript. Keep this window open.
# Terminal is the "responsible process" for AVFoundation mic access.

# Set Terminal window title so user knows not to close it
printf '\033]0;Recorder Daemon — Keep Open\007'

LOG=/tmp/recorder_daemon.log
DAEMON_DIR=/Users/christopherhastings/recorder

cd "$DAEMON_DIR" || { echo "ERROR: Cannot cd to $DAEMON_DIR"; exit 1; }
source venv/bin/activate

while true; do
    # Kill any leftover process holding the WebSocket port
    lsof -ti :8765 2>/dev/null | xargs kill -9 2>/dev/null || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting recorder daemon..." | tee -a "$LOG"
    python recorder_daemon.py 2>&1 | tee -a "$LOG"
    EXIT=${PIPESTATUS[0]}
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Daemon exited (code $EXIT). Restarting in 5s..." | tee -a "$LOG"
    sleep 5
done

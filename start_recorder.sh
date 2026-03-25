#!/bin/bash
# start_recorder.sh — Launch the recorder daemon under Terminal's permissions
# Add this script as a Login Item: System Settings → General → Login Items

# Kill any existing daemon on the port
lsof -ti :8765 2>/dev/null | xargs kill -9 2>/dev/null
sleep 1

cd ~/recorder
source venv/bin/activate
exec python recorder_daemon.py

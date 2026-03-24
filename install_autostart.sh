#!/bin/bash

# Configuration
USER_NAME=$(whoami)
PROJECT_DIR=$(pwd)
# Define PYTHON_PATH using realpath to ensure it's absolute and resolved
PYTHON_PATH="$(realpath "$PROJECT_DIR")/venv/bin/python"
AGENT_DIR="$HOME/Library/LaunchAgents"

# Ensure venv is created and dependencies are installed
echo "Ensuring Python virtual environment and dependencies are set up..."
if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$PROJECT_DIR/venv"
fi
echo "Installing/updating Python dependencies in venv..."
# Use the resolved PYTHON_PATH
"$PYTHON_PATH" -m pip install --upgrade pip
"$PYTHON_PATH" -m pip install -r "$PROJECT_DIR/requirements.txt"

# List of services to create
SERVICES=("daemon" "api" "menubar")
SCRIPTS=("recorder_daemon.py" "api_server.py" "menubar.py")

for i in "${!SERVICES[@]}"; do
    SERVICE=${SERVICES[$i]}
    SCRIPT=${SCRIPTS[$i]}
    PLIST="$AGENT_DIR/com.recorder.$SERVICE.plist"

    echo "Creating Agent for $SERVICE..."

    cat <<EOF > "$PLIST"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.recorder.$SERVICE</string>
$(if [ "$SERVICE" == "daemon" ]; then
    echo "    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>"
fi)
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_PATH}</string>
        <string>${PROJECT_DIR}/${SCRIPT}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>StandardOutPath</key>
    <string>/tmp/recorder_$SERVICE.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/recorder_$SERVICE.err</string>
</dict>
</plist>
EOF

    # Unload if already exists, then load
    launchctl unload "$PLIST" 2>/dev/null
    # Kill the process before loading the new plist
    if pgrep -f "$SCRIPT" > /dev/null; then
        echo "Killing existing $SCRIPT process..."
        pkill -f "$SCRIPT"
    fi
    launchctl load "$PLIST"
done

echo "Done! The tool suite will now start automatically on login."
echo "Check status in the menu bar."
#!/bin/bash

# Configuration
USER_NAME=$(whoami)
PROJECT_DIR=$(pwd)
PYTHON_PATH="$PROJECT_DIR/venv/bin/python"
AGENT_DIR="$HOME/Library/LaunchAgents"

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
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_PATH</string>
        <string>$PROJECT_DIR/$SCRIPT</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>StandardOutPath</key>
    <string>/tmp/recorder_$SERVICE.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/recorder_$SERVICE.err</string>
</dict>
</plist>
EOF

    # Unload if already exists, then load
    launchctl unload "$PLIST" 2>/dev/null
    launchctl load "$PLIST"
done

echo "Done! The tool suite will now start automatically on login."
echo "Check status in the menu bar."

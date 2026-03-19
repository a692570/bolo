#!/bin/bash

if [ -z "$TELNYX_API_KEY" ] && [ -f "$HOME/.codex/.env" ]; then
  export TELNYX_API_KEY="$(/usr/bin/awk -F= '/^TELNYX_API_KEY=/{print substr($0, index($0,$2)); exit}' "$HOME/.codex/.env")"
fi

if [ -z "$TELNYX_API_KEY" ] && [ -f "$HOME/.zshrc" ]; then
  export TELNYX_API_KEY="$(/usr/bin/sed -n 's/^export TELNYX_API_KEY=\"\\(.*\\)\"/\\1/p' "$HOME/.zshrc" | /usr/bin/head -n 1)"
fi

BOLO_DIR=/Users/abhisheksharma/bolo
LABEL=com.abhisheksharma.bolo
PLIST_FILE="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_EXEC=/Applications/Bolo.app/Contents/MacOS/Bolo
PYTHON_EXEC=/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python

mkdir -p "$HOME/Library/LaunchAgents"

pkill -f "bolo.py" 2>/dev/null
pkill -f "overlay.py" 2>/dev/null
sleep 1

PROGRAM_PATH="$APP_EXEC"
PROGRAM_ARGS=$(cat <<EOF
  <array>
    <string>$APP_EXEC</string>
  </array>
EOF
)

if [ ! -x "$PROGRAM_PATH" ]; then
  PROGRAM_PATH="$PYTHON_EXEC"
  PROGRAM_ARGS=$(cat <<EOF
  <array>
    <string>$PYTHON_EXEC</string>
    <string>$BOLO_DIR/bolo.py</string>
  </array>
EOF
)
fi

cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
$(printf '%s\n' "$PROGRAM_ARGS")
  <key>EnvironmentVariables</key>
  <dict>
    <key>TELNYX_API_KEY</key>
    <string>$TELNYX_API_KEY</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>$BOLO_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/bolo.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/bolo.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST_FILE"
launchctl kickstart -k "gui/$UID/$LABEL"

echo "Bolo started (LaunchAgent $LABEL)"
sleep 1
exit 0

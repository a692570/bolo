#!/bin/bash
set -e

echo "Installing Bolo - voice dictation for macOS"
echo "============================================"

if ! command -v python3 &>/dev/null; then
  echo "ERROR: Python 3 is required. Install from https://python.org"
  exit 1
fi

echo "Installing Python dependencies..."
pip3 install -r requirements.txt

if [ -z "$TELNYX_API_KEY" ]; then
  echo ""
  echo "You need a Telnyx API key to use Bolo."
  echo "Get one at https://telnyx.com"
  echo ""
  read -p "Paste your TELNYX_API_KEY here: " key
  echo ""
  echo "Adding to ~/.zshrc..."
  echo "export TELNYX_API_KEY=\"$key\"" >> ~/.zshrc
  export TELNYX_API_KEY="$key"
fi

BOLO_DIR="$(cd "$(dirname "$0")" && pwd)"
COMMAND_FILE="$BOLO_DIR/start-bolo.command"

cat > "$COMMAND_FILE" << SCRIPT
#!/bin/bash
export TELNYX_API_KEY="$TELNYX_API_KEY"
nohup /usr/bin/python3 "$BOLO_DIR/bolo.py" >> /tmp/bolo.log 2>&1 &
echo "Bolo started (PID \$!)"
sleep 1
exit 0
SCRIPT

chmod +x "$COMMAND_FILE"

osascript -e "tell application \"System Events\" to delete (every login item whose name contains \"bolo\")" 2>/dev/null || true
osascript -e "tell application \"System Events\" to make new login item at end of login items with properties {path:\"$COMMAND_FILE\", hidden:false}"

echo ""
echo "Done. Starting Bolo now..."
open "$COMMAND_FILE"
echo ""
echo "Bolo is running. You should see an icon in your menubar."
echo ""
echo "Grant two permissions when prompted or in System Settings:"
echo " 1. Accessibility"
echo " 2. Microphone"
echo ""
echo "Usage: Hold Right Option anywhere to dictate. Release to transcribe and paste."
echo "Logs: tail -f /tmp/bolo.log"

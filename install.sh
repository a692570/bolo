#!/bin/bash
set -e

echo "Installing Bolo — voice dictation for macOS"
echo "============================================"

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 is required. Install from https://python.org"
    exit 1
fi

# Install dependencies
echo "Installing Python dependencies..."
pip3 install -r requirements.txt

# Check for API key
if [ -z "$TELNYX_API_KEY" ]; then
    echo ""
    echo "You need a Telnyx API key to use Bolo."
    echo "Get one free at https://telnyx.com (no credit card required for AI APIs)"
    echo ""
    read -p "Paste your TELNYX_API_KEY here: " key
    echo ""
    echo "Adding to ~/.zshrc..."
    echo "export TELNYX_API_KEY=\"$key\"" >> ~/.zshrc
    export TELNYX_API_KEY="$key"
fi

# Install the launcher as a Login Item
BOLO_DIR="$(cd "$(dirname "$0")" && pwd)"
COMMAND_FILE="$BOLO_DIR/start-bolo.command"
chmod +x "$COMMAND_FILE"

# Add Login Item
osascript -e "tell application \"System Events\" to delete (every login item whose name contains \"bolo\")" 2>/dev/null || true
osascript -e "tell application \"System Events\" to make new login item at end of login items with properties {path:\"$COMMAND_FILE\", hidden:false}"

echo ""
echo "Done. Starting Bolo now..."
open "$COMMAND_FILE"

echo ""
echo "Bolo is running. You should see an icon in your menubar."
echo ""
echo "IMPORTANT: Grant two permissions when prompted (or in System Settings):"
echo "  1. Accessibility (for global hotkey)"
echo "  2. Microphone"
echo ""
echo "Usage: Hold Right Option anywhere to dictate. Release to transcribe and paste."
echo "Logs:  tail -f /tmp/bolo.log"

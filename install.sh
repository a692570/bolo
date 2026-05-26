#!/bin/bash
set -e

echo "Installing Bolo - voice dictation for macOS"
echo "============================================"

if ! command -v cargo &>/dev/null; then
  echo "ERROR: Rust/Cargo is required. Install from https://rustup.rs"
  exit 1
fi

if ! python3 - <<'PY' >/dev/null 2>&1
import AppKit
import Quartz
PY
then
  echo "Installing native macOS helper dependencies..."
  python3 -m pip install --user pyobjc-framework-AppKit pyobjc-framework-Quartz
fi

echo "Building Rust binary..."
cargo build --release

existing_key="${TELNYX_API_KEY:-}"
if [ -z "$existing_key" ] && [ -f "$HOME/.bolo/env" ]; then
  existing_key="$(grep '^TELNYX_API_KEY=' "$HOME/.bolo/env" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//')"
fi
if [ -z "$existing_key" ] && [ -f "$HOME/.codex/.env" ]; then
  existing_key="$(grep '^TELNYX_API_KEY=' "$HOME/.codex/.env" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//')"
fi

mkdir -p ~/.bolo

if [ -z "$existing_key" ]; then
  echo ""
  echo "You need a Telnyx API key to use Bolo."
  echo "Get one at https://telnyx.com"
  echo ""
  read -p "Paste your TELNYX_API_KEY here: " key
  echo ""
  existing_key="$key"
fi

if [ -n "$existing_key" ]; then
  echo "Writing TELNYX_API_KEY to ~/.bolo/env..."
  touch ~/.bolo/env
  if grep -q '^TELNYX_API_KEY=' ~/.bolo/env; then
    sed -i.bak "s|^TELNYX_API_KEY=.*|TELNYX_API_KEY=\"$existing_key\"|" ~/.bolo/env
  else
    echo "TELNYX_API_KEY=\"$existing_key\"" >> ~/.bolo/env
  fi
fi

BOLO_DIR="$(cd "$(dirname "$0")" && pwd)"
COMMAND_FILE="$BOLO_DIR/start-bolo.command"
chmod +x "$COMMAND_FILE" "$BOLO_DIR/start-bolo.sh" "$BOLO_DIR/restart.sh"

osascript -e "tell application \"System Events\" to delete (every login item whose name contains \"bolo\")" 2>/dev/null || true
osascript -e "tell application \"System Events\" to make new login item at end of login items with properties {path:\"$COMMAND_FILE\", hidden:false}"

echo ""
echo "Starting Bolo now..."
open "$COMMAND_FILE"
echo ""
echo "Done. Bolo is running from the Rust runtime."
echo ""
echo "Grant two permissions when prompted or in System Settings:"
echo " 1. Accessibility"
echo " 2. Microphone"
echo ""
echo "Usage: Hold Right Option anywhere to dictate. Release to transcribe and paste."
echo ""
echo "First run will ask you to pick a hotkey. You can change it later in ~/.bolo/env"
echo "Bolo automatically copies text to your clipboard. Nothing is lost."
echo "Restart later with: ./restart.sh"
echo "Logs: tail -f /tmp/bolo.log"

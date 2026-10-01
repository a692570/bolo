#!/bin/bash
set -euo pipefail

umask 077
BOLO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing Bolo - voice dictation for macOS"
echo "============================================"

if ! command -v cargo &>/dev/null; then
  echo "ERROR: Rust/Cargo is required. Install from https://rustup.rs"
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "ERROR: Python 3 is required. Install it from https://www.python.org/downloads/macos/"
  exit 1
fi

chmod +x "$BOLO_DIR/ensure-python-env.sh"
helper_python="$("$BOLO_DIR/ensure-python-env.sh" --sync)"

echo "Building Rust binary..."
cargo build --release

# Provider selection precedence: explicit BOLO_PROVIDER, then whichever single
# key is already present in the environment, then an interactive prompt that
# defaults to AssemblyAI.
provider="${BOLO_PROVIDER:-}"
if [ -z "$provider" ]; then
  if [ -n "${TELNYX_API_KEY:-}" ] && [ -z "${ASSEMBLYAI_API_KEY:-}" ]; then
    provider="telnyx"
  elif [ -n "${ASSEMBLYAI_API_KEY:-}" ] && [ -z "${TELNYX_API_KEY:-}" ]; then
    provider="assemblyai"
  fi
fi
if [ -z "$provider" ]; then
  echo ""
  echo "Choose the speech provider Bolo will use:"
  echo " 1. AssemblyAI (default). One key covers the dictation pipeline, optional"
  echo "    streaming, and the LLM gateway for voice rewrites."
  echo "    Get one at https://www.assemblyai.com/dashboard/home"
  echo " 2. Telnyx. Telnyx-hosted Deepgram nova-3 and inference APIs."
  echo "    Get one at https://telnyx.com"
  while [ -z "$provider" ]; do
    if ! IFS= read -r -p "Enter 1 or 2 [1]: " choice; then
      echo ""
      echo "Install cancelled. A provider choice is required."
      exit 1
    fi
    case "$choice" in
      1|"") provider="assemblyai" ;;
      2) provider="telnyx" ;;
      *) echo "Enter 1 or 2." ;;
    esac
  done
fi

existing_key="${TELNYX_API_KEY:-}"
if [ "$provider" = "assemblyai" ]; then
  existing_key="${ASSEMBLYAI_API_KEY:-}"
  if [ -z "$existing_key" ] && [ -f "$HOME/.bolo/env" ]; then
    existing_key="$(grep '^ASSEMBLYAI_API_KEY=' "$HOME/.bolo/env" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//' || true)"
  fi
else
  if [ -z "$existing_key" ] && [ -f "$HOME/.bolo/env" ]; then
    existing_key="$(grep '^TELNYX_API_KEY=' "$HOME/.bolo/env" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//' || true)"
  fi
  if [ -z "$existing_key" ] && [ -f "$HOME/.codex/.env" ]; then
    existing_key="$(grep '^TELNYX_API_KEY=' "$HOME/.codex/.env" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//' || true)"
  fi
fi

mkdir -p "$HOME/.bolo"
chmod 700 "$HOME/.bolo"

if [ "$provider" = "assemblyai" ]; then
  key_name="ASSEMBLYAI_API_KEY"
else
  key_name="TELNYX_API_KEY"
fi
if [ -z "$existing_key" ]; then
  echo ""
  echo "You need a $key_name to use Bolo."
  while [ -z "$existing_key" ]; do
    if ! IFS= read -r -s -p "Paste your ${key_name} here: " key; then
      echo ""
      echo "Install cancelled. An API key is required."
      exit 1
    fi
    echo ""
    if [[ "$key" =~ [^[:space:]] ]]; then
      existing_key="$key"
    else
      echo "API key is required. Paste a nonempty key or press Control-C to cancel."
    fi
  done
fi

echo "Writing ${key_name} to ~/.bolo/env..."
escaped_key="${existing_key//\\/\\\\}"
escaped_key="${escaped_key//\"/\\\"}"
env_file="$HOME/.bolo/env"
env_tmp="$(mktemp "$HOME/.bolo/env.tmp.XXXXXX")"
key_written=false
if [ -f "$env_file" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" == "$key_name="* ]]; then
      printf '%s="%s"\n' "$key_name" "$escaped_key" >> "$env_tmp"
      key_written=true
    else
      printf '%s\n' "$line" >> "$env_tmp"
    fi
  done < "$env_file"
fi
if [ "$key_written" = false ]; then
  printf '%s="%s"\n' "$key_name" "$escaped_key" >> "$env_tmp"
fi
chmod 600 "$env_tmp"
mv "$env_tmp" "$env_file"

COMMAND_FILE="$BOLO_DIR/start-bolo.command"
chmod +x "$COMMAND_FILE" "$BOLO_DIR/start-bolo.sh" "$BOLO_DIR/restart.sh" "$BOLO_DIR/update.sh" "$BOLO_DIR/ensure-python-env.sh"

if ! osascript - "$COMMAND_FILE" <<'APPLESCRIPT'
on run argv
  set commandPath to item 1 of argv
  tell application "System Events"
    repeat with loginItem in every login item
      try
        if path of loginItem is commandPath then delete loginItem
      end try
    end repeat
    make new login item at end of login items with properties {path:commandPath, hidden:false}
  end tell
end run
APPLESCRIPT
then
  echo "ERROR: Bolo could not register its login item."
  exit 1
fi

echo ""
echo "Starting Bolo now..."
if ! open "$COMMAND_FILE"; then
  echo "ERROR: Bolo could not be launched."
  exit 1
fi
RUNTIME_DIR="${BOLO_RUNTIME_DIR:-/tmp}"
BIN="$BOLO_DIR/target/release/bolo"
launch_verified=false
for _ in {1..20}; do
  supervisor_pid="$(cat "$RUNTIME_DIR/bolo-supervisor.pid" 2>/dev/null || true)"
  if [ -n "$supervisor_pid" ] && kill -0 "$supervisor_pid" 2>/dev/null && pgrep -f "$BIN" >/dev/null 2>&1; then
    launch_verified=true
    break
  fi
  sleep 0.25
done
if [ "$launch_verified" = false ]; then
  echo "ERROR: Bolo did not stay running. Check $RUNTIME_DIR/bolo.log"
  exit 1
fi
echo ""
echo "Done. Bolo is running from the Rust runtime."
echo ""
if [ "$provider" = "assemblyai" ]; then
  echo "Provider: AssemblyAI. Dictation runs on AssemblyAI's Dictation API and the"
  echo "same key powers optional streaming and the LLM gateway used for voice"
  echo "rewrites (enable model providers under Data Controls in the AssemblyAI"
  echo "dashboard if you use rewrites)."
else
  echo "Provider: Telnyx. Dictation runs on Telnyx-hosted Deepgram nova-3 and the"
  echo "Telnyx inference API handles LLM cleanup."
fi
echo "Grant two permissions when prompted or in System Settings:"
echo " 1. Accessibility for the Python interpreter Bolo uses:"
echo "    $helper_python"
echo "    Add that executable in Privacy & Security > Accessibility."
echo " 2. Microphone"
echo ""
echo "Usage: Hold your selected hotkey anywhere to dictate. Release to transcribe and paste."
echo ""
echo "First run will ask you to pick a hotkey. You can change it later in ~/.bolo/env"
echo "Bolo automatically copies text to your clipboard. Nothing is lost."
echo "Restart later with: ./restart.sh"
echo "Logs: tail -f /tmp/bolo.log"

#!/bin/bash

if [ -z "$TELNYX_API_KEY" ] && [ -f "$HOME/.codex/.env" ]; then
  export TELNYX_API_KEY="$(/usr/bin/awk -F= '/^TELNYX_API_KEY=/{print substr($0, index($0,$2)); exit}' "$HOME/.codex/.env")"
fi

if [ -z "$TELNYX_API_KEY" ] && [ -f "$HOME/.zshrc" ]; then
  export TELNYX_API_KEY="$(/usr/bin/sed -n 's/^export TELNYX_API_KEY=\"\\(.*\\)\"/\\1/p' "$HOME/.zshrc" | /usr/bin/head -n 1)"
fi

LOOP_PID_FILE=/tmp/bolo-loop.pid
LOCK_DIR=/tmp/bolo-supervisor.lock
BOLO_DIR=/Users/abhisheksharma/bolo

# Kill existing loop and any running bolo/overlay processes
if [ -f "$LOOP_PID_FILE" ]; then
  kill $(cat "$LOOP_PID_FILE") 2>/dev/null
  rm -f "$LOOP_PID_FILE"
fi
pkill -f "bolo.py" 2>/dev/null
pkill -f "overlay.py" 2>/dev/null
sleep 1

# Start fresh restart loop
nohup /bin/bash -c '
  LOCK_DIR="$1"
  LOOP_PID_FILE="$2"
  BOLO_DIR="$3"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    exit 0
  fi
  trap '\''rm -rf "$LOCK_DIR"; rm -f "$LOOP_PID_FILE"'\'' EXIT
  while true; do
    echo "[bolo] starting at $(date)" >> /tmp/bolo.log
    /usr/bin/python3 "$BOLO_DIR/bolo.py" >> /tmp/bolo.log 2>&1
    echo "[bolo] crashed — restarting in 3s" >> /tmp/bolo.log
    sleep 3
  done
' _ "$LOCK_DIR" "$LOOP_PID_FILE" "$BOLO_DIR" >/dev/null 2>&1 &

SUP_PID=$!
echo "$SUP_PID" > "$LOOP_PID_FILE"
echo "Bolo started (loop PID $SUP_PID)"
sleep 1
exit 0

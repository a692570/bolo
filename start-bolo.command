#!/bin/bash
source ~/.zshrc 2>/dev/null || true

LOOP_PID_FILE=/tmp/bolo-loop.pid
LOCK_DIR=/tmp/bolo-supervisor.lock

# Kill existing loop and any running bolo/overlay processes
if [ -f "$LOOP_PID_FILE" ]; then
  kill $(cat "$LOOP_PID_FILE") 2>/dev/null
  rm -f "$LOOP_PID_FILE"
fi
pkill -f "bolo.py" 2>/dev/null
pkill -f "overlay.py" 2>/dev/null
sleep 1

# Start fresh restart loop
(
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    exit 0
  fi
  trap 'rm -rf "$LOCK_DIR"' EXIT
  while true; do
    echo "[bolo] starting at $(date)" >> /tmp/bolo.log
    /usr/bin/python3 /Users/abhisheksharma/bolo/bolo.py >> /tmp/bolo.log 2>&1
    echo "[bolo] crashed — restarting in 3s" >> /tmp/bolo.log
    sleep 3
  done
) &

echo $! > "$LOOP_PID_FILE"
echo "Bolo started (loop PID $!)"
sleep 1
exit 0

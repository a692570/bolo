#!/bin/bash
# Bolo launcher -- designed to run as a Login Item (opens in Terminal).
# Terminal.app must have Accessibility + Microphone permissions in System Settings.
# Running python3 from Terminal inherits those permissions.

BOLO_DIR=/Users/abhisheksharma/bolo
LOCK_DIR=/tmp/bolo-supervisor.lock
PID_FILE=/tmp/bolo-supervisor.pid

# Prevent duplicate supervisors
if mkdir "$LOCK_DIR" 2>/dev/null; then
    trap "rmdir '$LOCK_DIR' 2>/dev/null; exit" EXIT INT TERM
else
    echo "[bolo] supervisor already running (lock exists). Exiting."
    exit 0
fi

pkill -f "bolo.py" 2>/dev/null || true
pkill -f "overlay.py" 2>/dev/null || true
sleep 1

# Run supervisor loop in background so Terminal can close
(
    while true; do
        python3 "$BOLO_DIR/bolo.py" >> /tmp/bolo.log 2>&1
        EXIT_CODE=$?
        echo "[bolo] exited with code $EXIT_CODE -- restarting in 5s" >> /tmp/bolo.log
        pkill -f "bolo.py" 2>/dev/null || true
        sleep 5
    done
) &

SUP_PID=$!
echo "$SUP_PID" > "$PID_FILE"
echo "[bolo] supervisor started (PID $SUP_PID)"
sleep 1
exit 0

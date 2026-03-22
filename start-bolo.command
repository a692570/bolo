#!/bin/bash
# Bolo launcher -- designed to run as a Login Item (opens in Terminal).
# Terminal.app must have Accessibility + Microphone permissions in System Settings.
# Running python3 from Terminal inherits those permissions.

BOLO_DIR=/Users/abhisheksharma/bolo
LOCK_DIR=/tmp/bolo-supervisor.lock

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
        echo "[bolo] crashed -- restarting in 3s" >> /tmp/bolo.log
        sleep 3
    done
) &

echo "[bolo] supervisor started (PID $!)"
sleep 1
exit 0

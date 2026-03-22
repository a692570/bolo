#!/bin/bash
# Restart Bolo -- must be run from a Terminal session (for Accessibility inheritance).

DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK_DIR=/tmp/bolo-supervisor.lock
PID_FILE=/tmp/bolo-supervisor.pid

# Kill existing supervisor loop by saved PID
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    kill "$OLD_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
fi

# Kill any remaining bolo processes AND their parent supervisor loops
pgrep -f "bolo.py" | xargs -I{} ps -o ppid= -p {} 2>/dev/null | sort -u | xargs kill 2>/dev/null || true
pkill -f "bolo.py" 2>/dev/null || true
pkill -f "overlay.py" 2>/dev/null || true

# Remove stale lock
rmdir "$LOCK_DIR" 2>/dev/null || rm -rf "$LOCK_DIR" 2>/dev/null || true

sleep 1

# Start a fresh supervisor loop in background
(
    mkdir "$LOCK_DIR" 2>/dev/null || true
    while true; do
        python3 "$DIR/bolo.py" >> /tmp/bolo.log 2>&1
        echo "[bolo] crashed -- restarting in 5s" >> /tmp/bolo.log
        pkill -f "bolo.py" 2>/dev/null || true
        sleep 5
    done
) &

SUP_PID=$!
echo "$SUP_PID" > "$PID_FILE"
echo "[bolo] restarted (supervisor PID $SUP_PID)"

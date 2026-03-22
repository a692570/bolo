#!/bin/bash
# Restart Bolo -- must be run from a Terminal session (for Accessibility inheritance).

DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK_DIR=/tmp/bolo-supervisor.lock

# Kill existing supervisor and bolo processes
pkill -f "bolo.py" 2>/dev/null || true
pkill -f "overlay.py" 2>/dev/null || true

# Remove stale supervisor lock so start-bolo.command can acquire it
rmdir "$LOCK_DIR" 2>/dev/null || true

sleep 1

# Start a fresh supervisor loop in background
(
    mkdir "$LOCK_DIR" 2>/dev/null || true
    while true; do
        python3 "$DIR/bolo.py" >> /tmp/bolo.log 2>&1
        echo "[bolo] crashed -- restarting in 3s" >> /tmp/bolo.log
        sleep 3
    done
) &

echo "[bolo] restarted (supervisor PID $!)"

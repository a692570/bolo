#!/bin/bash
# Bolo launcher -- designed to run as a Login Item (opens in Terminal).
# Terminal.app must have Accessibility + Microphone permissions in System Settings.
# Running python3 from Terminal inherits those permissions.

BOLO_DIR=/Users/abhisheksharma/bolo
LOCK_DIR=/tmp/bolo-supervisor.lock
PID_FILE=/tmp/bolo-supervisor.pid
LOG=/tmp/bolo.log

# Prevent duplicate supervisors
if mkdir "$LOCK_DIR" 2>/dev/null; then
    trap "rmdir '$LOCK_DIR' 2>/dev/null; exit" EXIT INT TERM
else
    echo "[bolo] supervisor already running (lock exists). Exiting."
    exit 0
fi

# Clear any stale locks so a fresh start is always clean
rm -rf /tmp/bolo-instance.lock 2>/dev/null || true

pkill -f "bolo.py" 2>/dev/null || true
pkill -f "overlay.py" 2>/dev/null || true
sleep 1

# Load API key from .codex/.env (authoritative source).
# Unset any shell env var so bolo.py's own loader always wins from the file.
unset TELNYX_API_KEY
_KEY=$(grep '^TELNYX_API_KEY=' ~/.codex/.env 2>/dev/null | head -1 | cut -d= -f2-)
if [ -z "$_KEY" ]; then
    echo "[bolo] ERROR: TELNYX_API_KEY not found in ~/.codex/.env" >> "$LOG"
    exit 1
fi
export TELNYX_API_KEY="$_KEY"

# Run supervisor loop in background so Terminal can close.
# Exit codes that must NOT trigger a respawn:
#   0   — clean quit (user chose Quit Bolo)
#   1   — fatal startup error (bad key, missing dep); respawning would just loop
#   143 — SIGTERM (deliberate kill)
(
    while true; do
        python3 "$BOLO_DIR/bolo.py" >> "$LOG" 2>&1
        EXIT_CODE=$?
        case $EXIT_CODE in
            0)
                echo "[bolo] clean exit — not restarting" >> "$LOG"
                break
                ;;
            1)
                echo "[bolo] fatal startup error (exit 1) — not restarting; check $LOG" >> "$LOG"
                break
                ;;
            137|143)
                echo "[bolo] received SIGKILL/SIGTERM — not restarting" >> "$LOG"
                rm -rf /tmp/bolo-instance.lock 2>/dev/null || true
                break
                ;;
            *)
                echo "[bolo] exited with code $EXIT_CODE — restarting in 5s" >> "$LOG"
                # Clear stale instance lock before restart
                rm -rf /tmp/bolo-instance.lock 2>/dev/null || true
                pkill -f "overlay.py" 2>/dev/null || true
                sleep 5
                ;;
        esac
    done
) &

SUP_PID=$!
echo "$SUP_PID" > "$PID_FILE"
echo "[bolo] supervisor started (PID $SUP_PID)"
sleep 1
exit 0

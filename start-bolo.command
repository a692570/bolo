#!/bin/bash
# Bolo launcher designed to run as a macOS Login Item through Terminal.

BOLO_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG=/tmp/bolo.log
LOCK_DIR=/tmp/bolo-supervisor.lock
PID_FILE=/tmp/bolo-supervisor.pid
BIN="$BOLO_DIR/target/release/bolo"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ "$existing_pid" != "" ] && kill -0 "$existing_pid" 2>/dev/null; then
        echo "[bolo] supervisor already running"
        exit 0
    fi
    rm -rf "$LOCK_DIR" "$PID_FILE" 2>/dev/null || true
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "[bolo] supervisor already running"
        exit 0
    fi
fi

cd "$BOLO_DIR" || exit 1

if [ "${BOLO_AUTO_UPDATE:-on}" != "off" ] && [ -x "$BOLO_DIR/update.sh" ]; then
    echo "[bolo] checking for updates" >> "$LOG"
    "$BOLO_DIR/update.sh" >> "$LOG" 2>&1 || echo "[bolo] update check failed" >> "$LOG"
fi

if [ ! -x "$BIN" ] || [ "$BOLO_DIR/src/main.rs" -nt "$BIN" ] || [ "$BOLO_DIR/Cargo.toml" -nt "$BIN" ]; then
    if ! command -v cargo >/dev/null 2>&1; then
        echo "[bolo] ERROR: cargo not found. Run ./install.sh first." >> "$LOG"
        exit 1
    fi
    echo "[bolo] building Rust runtime" >> "$LOG"
    cargo build --release >> "$LOG" 2>&1 || exit 1
fi

pkill -f "$BOLO_DIR/bolo.py" 2>/dev/null || true
pkill -f "$BOLO_DIR/hotkey.py" 2>/dev/null || true
pkill -f "$BOLO_DIR/overlay.py" 2>/dev/null || true

export BIN LOG LOCK_DIR PID_FILE
nohup bash -c '
    cleanup() {
        rm -rf "$LOCK_DIR" "$PID_FILE" /tmp/bolo-instance.lock 2>/dev/null || true
    }
    trap cleanup EXIT INT TERM
    while true; do
        "$BIN" >> "$LOG" 2>&1
        EXIT_CODE=$?
        case $EXIT_CODE in
            0)
                echo "[bolo] clean exit" >> "$LOG"
                break
                ;;
            1)
                echo "[bolo] startup error, not restarting" >> "$LOG"
                break
                ;;
            137|143)
                echo "[bolo] terminated" >> "$LOG"
                rm -rf /tmp/bolo-instance.lock 2>/dev/null || true
                break
                ;;
            *)
                echo "[bolo] exited with code $EXIT_CODE, restarting in 5s" >> "$LOG"
                rm -rf /tmp/bolo-instance.lock 2>/dev/null || true
                sleep 5
                ;;
        esac
    done
' >/dev/null 2>&1 &

SUP_PID=$!
echo "$SUP_PID" > "$PID_FILE"
echo "[bolo] supervisor started (PID $SUP_PID)"
sleep 1
exit 0

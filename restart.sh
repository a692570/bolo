#!/bin/bash
# Restart Bolo through the same launcher used by the Login Item.

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$DIR/target/release/bolo"
COMMAND_FILE="$DIR/start-bolo.command"
RUNTIME_DIR="${BOLO_RUNTIME_DIR:-/tmp}"
LOCK_DIR="$RUNTIME_DIR/bolo-supervisor.lock"
PID_FILE="$RUNTIME_DIR/bolo-supervisor.pid"
LOG="$RUNTIME_DIR/bolo.log"

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    kill "$OLD_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
fi

pkill -f "$BIN" 2>/dev/null || true
pkill -f "$DIR/bolo.py" 2>/dev/null || true
pkill -f "$DIR/hotkey.py" 2>/dev/null || true
pkill -f "$DIR/overlay.py" 2>/dev/null || true
rm -f "$RUNTIME_DIR/bolo.pid" 2>/dev/null || true
rm -rf "$LOCK_DIR" /tmp/bolo-instance.lock 2>/dev/null || true

sleep 1

if [ ! -x "$BIN" ] || [ "$DIR/src/main.rs" -nt "$BIN" ] || [ "$DIR/Cargo.toml" -nt "$BIN" ]; then
    if ! (cd "$DIR" && cargo build --release >> "$LOG" 2>&1); then
        echo "[bolo] rebuild failed. Check $LOG" >&2
        exit 1
    fi
fi

chmod +x "$COMMAND_FILE"
if ! open "$COMMAND_FILE"; then
    echo "[bolo] restart failed: launcher could not be opened" >&2
    exit 1
fi
restart_verified=false
for _ in {1..20}; do
    supervisor_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$supervisor_pid" ] && kill -0 "$supervisor_pid" 2>/dev/null && pgrep -f "$BIN" >/dev/null 2>&1; then
        restart_verified=true
        break
    fi
    sleep 0.25
done
if [ "$restart_verified" = false ]; then
    echo "[bolo] restart failed. Check $LOG" >&2
    exit 1
fi
echo "[bolo] restart complete"

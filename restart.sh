#!/bin/bash
# Restart Bolo through the same launcher used by the Login Item.

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$DIR/target/release/bolo"
COMMAND_FILE="$DIR/start-bolo.command"
LOCK_DIR=/tmp/bolo-supervisor.lock
PID_FILE=/tmp/bolo-supervisor.pid

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    kill "$OLD_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
fi

pkill -f "$BIN" 2>/dev/null || true
pkill -f "$DIR/bolo.py" 2>/dev/null || true
pkill -f "$DIR/overlay.py" 2>/dev/null || true
rm -f /tmp/bolo.pid 2>/dev/null || true
rm -rf "$LOCK_DIR" /tmp/bolo-instance.lock 2>/dev/null || true

sleep 1

if [ ! -x "$BIN" ] || [ "$DIR/src/main.rs" -nt "$BIN" ] || [ "$DIR/Cargo.toml" -nt "$BIN" ]; then
    (cd "$DIR" && cargo build --release >> /tmp/bolo.log 2>&1)
fi

chmod +x "$COMMAND_FILE"
open "$COMMAND_FILE"
echo "[bolo] restart requested"

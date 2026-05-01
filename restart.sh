#!/bin/bash
# Restart Bolo -- must be run from a Terminal session for permissions inheritance.

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$DIR/target/release/bolo"

pkill -f "$BIN" 2>/dev/null || true

sleep 1

if [ ! -x "$BIN" ]; then
    (cd "$DIR" && cargo build --release >> /tmp/bolo.log 2>&1)
fi

"$BIN" >> /tmp/bolo.log 2>&1 &
echo "[bolo] restart requested"

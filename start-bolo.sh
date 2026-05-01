#!/bin/bash
# Start the Rust Bolo runtime from the current user session.

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$DIR/target/release/bolo"

cd "$DIR" || exit 1

if [ ! -x "$BIN" ] || [ "$DIR/src/main.rs" -nt "$BIN" ] || [ "$DIR/Cargo.toml" -nt "$BIN" ]; then
    cargo build --release || exit 1
fi

exec "$BIN"

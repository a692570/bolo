#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

pkill -f "bolo.py" 2>/dev/null || true
pkill -f "overlay.py" 2>/dev/null || true
rm -f /tmp/bolo-loop.pid

open "$DIR/start-bolo.command"

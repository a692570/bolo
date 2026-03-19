#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

pkill -f "start-bolo.command" 2>/dev/null || true
pkill -f "bolo.py" 2>/dev/null || true
pkill -f "overlay.py" 2>/dev/null || true
rm -f /tmp/bolo-loop.pid
rm -rf /tmp/bolo-supervisor.lock

/bin/bash "$DIR/start-bolo.command" >/dev/null 2>&1 &

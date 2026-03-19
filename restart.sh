#!/bin/bash
# Restart Bolo — must be run from a Terminal session (for Accessibility inheritance).
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

pkill -f "bolo.py" 2>/dev/null || true
pkill -f "overlay.py" 2>/dev/null || true
sleep 1

nohup python3 "$DIR/bolo.py" > /tmp/bolo.log 2>&1 &
echo "Bolo restarted (PID $!)"

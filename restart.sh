#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.abhisheksharma.bolo
PLIST_FILE="$HOME/Library/LaunchAgents/$LABEL.plist"

pkill -f "start-bolo.command" 2>/dev/null || true
pkill -f "bolo.py" 2>/dev/null || true
pkill -f "overlay.py" 2>/dev/null || true
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$PLIST_FILE"

/bin/bash "$DIR/start-bolo.command" >/dev/null 2>&1 &

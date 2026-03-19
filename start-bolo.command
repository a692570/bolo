#!/bin/bash
# Bolo launcher — designed to run as a Login Item (opens in Terminal).
# Terminal.app must have Accessibility + Microphone permissions in System Settings.
# Running python3 from Terminal inherits those permissions.

BOLO_DIR=/Users/abhisheksharma/bolo

pkill -f "bolo.py" 2>/dev/null || true
pkill -f "overlay.py" 2>/dev/null || true
sleep 1

nohup python3 "$BOLO_DIR/bolo.py" > /tmp/bolo.log 2>&1 &
sleep 2
exit 0

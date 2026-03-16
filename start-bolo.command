#!/bin/bash
source ~/.zshrc 2>/dev/null || true

# Auto-restart loop — Bolo restarts automatically if it crashes
(
  while true; do
    echo "[bolo] starting at $(date)" >> /tmp/bolo.log
    /usr/bin/python3 /Users/abhisheksharma/bolo/bolo.py >> /tmp/bolo.log 2>&1
    echo "[bolo] crashed or exited at $(date) — restarting in 3s" >> /tmp/bolo.log
    sleep 3
  done
) &

echo "Bolo started (PID $!)"
sleep 1
exit 0

#!/bin/bash
source ~/.zshrc 2>/dev/null || true

# Hard guarantee: only one bolo.py running
pkill -f "bolo.py" 2>/dev/null
sleep 1

echo "[bolo] starting at $(date)" >> /tmp/bolo.log

(
  while true; do
    /usr/bin/python3 /Users/abhisheksharma/bolo/bolo.py >> /tmp/bolo.log 2>&1
    echo "[bolo] crashed — restarting in 3s" >> /tmp/bolo.log
    sleep 3
  done
) &

echo "Bolo started (PID $!)"
sleep 1
exit 0

#!/bin/bash
source ~/.zshrc 2>/dev/null || true

LOCKFILE=/tmp/bolo.lock

# If already running, do nothing
if [ -f "$LOCKFILE" ] && kill -0 $(cat "$LOCKFILE") 2>/dev/null; then
  echo "Bolo already running (PID $(cat $LOCKFILE))"
  exit 0
fi

echo "Starting Bolo..."

(
  while true; do
    echo "[bolo] starting at $(date)" >> /tmp/bolo.log
    /usr/bin/python3 /Users/abhisheksharma/bolo/bolo.py >> /tmp/bolo.log 2>&1
    # Only restart if lockfile still exists (not a clean quit)
    if [ ! -f "$LOCKFILE" ]; then
      break
    fi
    echo "[bolo] crashed — restarting in 3s" >> /tmp/bolo.log
    sleep 3
  done
) &

echo $! > "$LOCKFILE"
echo "Bolo started (PID $!)"
sleep 1
exit 0

#!/bin/bash
source ~/.zshrc 2>/dev/null || true
nohup /usr/bin/python3 /Users/abhisheksharma/bolo/bolo.py >> /tmp/bolo.log 2>&1 &
echo "Bolo started (PID $!)"
sleep 1
exit 0

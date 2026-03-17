#!/bin/bash
# Bolo launcher — runs in user session so mic works
cd "$(dirname "$0")"
exec /usr/bin/python3 bolo.py >> /tmp/bolo.log 2>&1

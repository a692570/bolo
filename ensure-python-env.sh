#!/bin/bash
set -euo pipefail

BOLO_DIR="$(cd "$(dirname "$0")" && pwd)"
BOLO_HOME="$HOME/.bolo"
VENV_DIR="${BOLO_VENV_DIR:-$BOLO_HOME/venv}"
SYNC_REQUIREMENTS=false
if [ "${1:-}" = "--sync" ]; then
    SYNC_REQUIREMENTS=true
elif [ -n "${1:-}" ]; then
    echo "Usage: ./ensure-python-env.sh [--sync]" >&2
    exit 2
fi

verify_helpers() {
    "$1" -c 'import objc, AppKit, Foundation, Quartz, ApplicationServices' >/dev/null 2>&1
}

if [ -n "${BOLO_PYTHON:-}" ]; then
    if [ ! -x "$BOLO_PYTHON" ] || ! verify_helpers "$BOLO_PYTHON"; then
        echo "ERROR: BOLO_PYTHON cannot load Bolo's macOS helper packages: $BOLO_PYTHON" >&2
        exit 1
    fi
    printf '%s\n' "$BOLO_PYTHON"
    exit 0
fi

if [ "$SYNC_REQUIREMENTS" = false ] && [ -x "$VENV_DIR/bin/python3" ] && verify_helpers "$VENV_DIR/bin/python3"; then
    printf '%s\n' "$VENV_DIR/bin/python3"
    exit 0
fi

BOOTSTRAP_PYTHON="${BOLO_PYTHON_BOOTSTRAP:-python3}"
if ! command -v "$BOOTSTRAP_PYTHON" >/dev/null 2>&1; then
    echo "ERROR: Python 3 is required. Install it from https://www.python.org/downloads/macos/" >&2
    exit 1
fi

umask 077
mkdir -p "$BOLO_HOME"
chmod 700 "$BOLO_HOME"

echo "Installing Bolo's macOS helper environment..." >&2
if [ ! -x "$VENV_DIR/bin/python3" ]; then
    "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python3" -m pip install --disable-pip-version-check -r "$BOLO_DIR/helper-requirements.txt" >&2

if ! verify_helpers "$VENV_DIR/bin/python3"; then
    echo "ERROR: Bolo's macOS helper environment could not be verified." >&2
    exit 1
fi

printf '%s\n' "$VENV_DIR/bin/python3"

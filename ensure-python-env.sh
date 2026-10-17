#!/bin/bash
set -euo pipefail

BOLO_DIR="$(cd "$(dirname "$0")" && pwd)"
BOLO_HOME="$HOME/.bolo"
VENV_DIR="${BOLO_VENV_DIR:-$BOLO_HOME/venv}"
SYNC_REQUIREMENTS=false
if [ "${1:-}" = "--sync" ]; then
    SYNC_REQUIREMENTS=true
elif [ "${1:-}" = "--parakeet" ]; then
    # Opt-in mode: create/refresh the offline-transcription venv for the
    # parakeet rung and exit. Never run at app startup: the helper path
    # below must stay fast, and parakeet (MLX + 2.4GB weights in the HF
    # cache) is a one-time, user-initiated install.
    BOLO_PARAKEET_VENV="${BOLO_PARAKEET_VENV:-$HOME/.bolo/parakeet-venv}"
    mkdir -p "$HOME/.bolo"
    # parakeet-mlx 0.5.3 needs Python >= 3.10; probe newest first.
    PARAKEET_BOOTSTRAP="${BOLO_PARAKEET_BOOTSTRAP:-}"
    if [ -z "$PARAKEET_BOOTSTRAP" ]; then
        for candidate in python3.13 python3.12 python3.11 python3; do
            if command -v "$candidate" >/dev/null 2>&1 \
                && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
                PARAKEET_BOOTSTRAP="$candidate"
                break
            fi
        done
    fi
    if [ -z "$PARAKEET_BOOTSTRAP" ]; then
        echo "ERROR: the parakeet rung needs Python >= 3.10 (parakeet-mlx 0.5.3 requirement)." >&2
        echo "Install one from https://www.python.org/downloads/macos/ or set BOLO_PARAKEET_BOOTSTRAP." >&2
        exit 1
    fi
    echo "Installing Bolo's parakeet transcription environment with $PARAKEET_BOOTSTRAP..." >&2
    if [ ! -x "$BOLO_PARAKEET_VENV/bin/python3" ]; then
        "$PARAKEET_BOOTSTRAP" -m venv "$BOLO_PARAKEET_VENV"
    fi
    "$BOLO_PARAKEET_VENV/bin/python3" -m pip install --disable-pip-version-check \
        -r "$BOLO_DIR/parakeet-requirements.txt" >&2
    if ! "$BOLO_PARAKEET_VENV/bin/python3" -c 'import parakeet_mlx' >/dev/null 2>&1; then
        echo "ERROR: the parakeet venv exists but parakeet_mlx does not import: $BOLO_PARAKEET_VENV" >&2
        exit 1
    fi
    printf '%s\n' "$BOLO_PARAKEET_VENV/bin/python3"
    exit 0
elif [ -n "${1:-}" ]; then
    echo "Usage: ./ensure-python-env.sh [--sync] [--parakeet]" >&2
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

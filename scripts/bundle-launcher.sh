#!/bin/bash
# Launcher inside Bolo.app (Contents/MacOS/bolo).
#
# Mirrors start-bolo.command's supervisor pattern (lock dir + pid file +
# relaunch-on-crash under BOLO_RUNTIME_DIR, defaulting to /tmp) with paths
# resolved relative to the bundle instead of a source checkout:
#
#   1. pick the matching bundled-python arch dir for this machine
#   2. on first run, create the helper venv from the bundled python and
#      install the bundled wheels fully offline
#   3. export BOLO_BUNDLE_MODE=1 + BOLO_PYTHON so the runtime knows it is
#      running from the bundle and which interpreter to spawn
#   4. refuse a second instance with a clean notification
#   5. register Bolo as a login item (once, via System Events)
#   6. self-heal the ad-hoc code signature if the app was modified
#   7. supervise the Rust binary, restarting after crashes
#
# The launcher intentionally never builds anything: a source checkout is not
# required, which is the whole point of the DMG.

set -u

APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
RESOURCES="$APP_DIR/Contents/Resources"
BIN="$APP_DIR/Contents/MacOS/bolo-runtime"
RUNTIME_DIR="${BOLO_RUNTIME_DIR:-/tmp}"
LOG="$RUNTIME_DIR/bolo.log"
LOCK_DIR="$RUNTIME_DIR/bolo-supervisor.lock"
PID_FILE="$RUNTIME_DIR/bolo-supervisor.pid"
VENV_DIR="${BOLO_VENV_DIR:-$HOME/.bolo/venv}"
mkdir -p "$RUNTIME_DIR" 2>/dev/null || true

# --- Pure helpers (unit-tested via BOLO_PRINT_STATE) -------------------------

bolo_pick_arch() {
    # $1: output of uname -m -> bundled python directory name.
    case "$1" in
        arm64) printf '%s\n' "aarch64" ;;
        x86_64) printf '%s\n' "x86_64" ;;
        *)
            printf '%s\n' "unsupported"
            return 1
            ;;
    esac
}

verify_helpers() {
    "$1" -c 'import objc, AppKit, Foundation, Quartz, ApplicationServices' >/dev/null 2>&1
}

# --- Resolve the machine's bundled python ------------------------------------

ARCH_DIR="$(bolo_pick_arch "$(uname -m)")" || ARCH_DIR="unsupported"
BUNDLED_PYTHON_BIN="$RESOURCES/python/$ARCH_DIR/bin"
ARCH_DIR_OK=0
if [ "$ARCH_DIR" != "unsupported" ] && [ -x "$BUNDLED_PYTHON_BIN/python3.12" ]; then
    ARCH_DIR_OK=1
else
    # One retry before declaring failure: the app can be launched while its
    # copy is still in flight (drag mid-transfer) or while the DMG volume is
    # detaching, and both surface here as a missing binary that exists a
    # second later.
    sleep 2
    if [ "$ARCH_DIR" != "unsupported" ] && [ -x "$BUNDLED_PYTHON_BIN/python3.12" ]; then
        ARCH_DIR_OK=1
    fi
fi
if [ "$ARCH_DIR_OK" != 1 ]; then
    echo "[bolo] ERROR: bundled helper runtime unusable ($ARCH_DIR, $BUNDLED_PYTHON_BIN/python3.12)." >> "$LOG" 2>/dev/null || true
    if [ "$ARCH_DIR" = "unsupported" ]; then
        osascript -e 'display notification "Bolo does not support this Mac architecture yet." with title "Bolo"' >/dev/null 2>&1 || true
    else
        osascript -e 'display notification "Bolo'\''s helper runtime is missing from this copy. Reinstall from the latest Bolo DMG." with title "Bolo"' >/dev/null 2>&1 || true
    fi
    exit 1
fi

# --- Test hook: print resolved state, touch nothing ---------------------------

if [ "${BOLO_PRINT_STATE:-0}" = "1" ]; then
    if verify_helpers "$VENV_DIR/bin/python3"; then VENV_OK=1; else VENV_OK=0; fi
    if [ -d "$LOCK_DIR" ]; then
        existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ "$existing_pid" != "" ] && kill -0 "$existing_pid" 2>/dev/null; then
            ALREADY_RUNNING=1
        else
            ALREADY_RUNNING=0
        fi
    else
        ALREADY_RUNNING=0
    fi
    printf 'ARCH_DIR=%s\n' "$ARCH_DIR"
    printf 'BUNDLED_PYTHON_BIN=%s\n' "$BUNDLED_PYTHON_BIN"
    printf 'VENV_DIR=%s\n' "$VENV_DIR"
    printf 'VENV_OK=%s\n' "$VENV_OK"
    printf 'APP_DIR=%s\n' "$APP_DIR"
    printf 'RESOURCES=%s\n' "$RESOURCES"
    printf 'RUNTIME_DIR=%s\n' "$RUNTIME_DIR"
    printf 'ALREADY_RUNNING=%s\n' "$ALREADY_RUNNING"
    printf 'BUNDLE_MODE=%s\n' "${BOLO_BUNDLE_MODE:-1}"
    printf 'LOGIN_ITEM_SKIPPED=%s\n' "${BOLO_SKIP_LOGIN_ITEM:-0}"
    exit 0
fi

# --- First-run helper venv from the bundled runtime ---------------------------

# A venv that already imports the helper stack is never touched, so a source
# install's ~/.bolo/venv survives a bundle install (and vice versa). Only a
# missing or broken venv is (re)created, from the bundled python + wheels, no
# network required.
if ! [ -x "$VENV_DIR/bin/python3" ] || ! verify_helpers "$VENV_DIR/bin/python3"; then
    echo "[bolo] preparing the Bolo helper environment (first run)..." >> "$LOG" 2>/dev/null || true
    rm -rf "$VENV_DIR"
    if ! "$BUNDLED_PYTHON_BIN/python3.12" -m venv "$VENV_DIR" >> "$LOG" 2>&1; then
        echo "[bolo] ERROR: could not create the helper venv at $VENV_DIR." >> "$LOG"
        osascript -e 'display notification "Bolo could not set up its helper environment. Check the log for details." with title "Bolo"' >/dev/null 2>&1 || true
        exit 1
    fi
    if ! "$VENV_DIR/bin/python3" -m pip install --no-index --no-input \
        --disable-pip-version-check \
        --find-links "$RESOURCES/wheels" \
        -r "$RESOURCES/helper-requirements.txt" >> "$LOG" 2>&1; then
        echo "[bolo] ERROR: bundled helper wheels failed to install." >> "$LOG"
        osascript -e 'display notification "Bolo could not set up its helper environment. Check the log for details." with title "Bolo"' >/dev/null 2>&1 || true
        exit 1
    fi
fi

export BOLO_BUNDLE_MODE=1
export BOLO_PYTHON="$VENV_DIR/bin/python3"

# --- Single instance ------------------------------------------------------------

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ "$existing_pid" != "" ] && kill -0 "$existing_pid" 2>/dev/null; then
        echo "[bolo] already running (supervisor PID $existing_pid)" >> "$LOG"
        osascript -e 'display notification "Bolo is already running. Look for the Bolo icon in the menu bar." with title "Bolo"' >/dev/null 2>&1 || true
        exit 0
    fi
    rm -rf "$LOCK_DIR" "$PID_FILE" 2>/dev/null || true
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "[bolo] supervisor lock is held by another process" >> "$LOG"
        osascript -e 'display notification "Bolo is already running. Look for the Bolo icon in the menu bar." with title "Bolo"' >/dev/null 2>&1 || true
        exit 0
    fi
fi

# --- Friendly nudge when launched straight from the mounted DMG -----------------

case "$APP_DIR" in
    /Volumes/*)
        osascript -e 'display notification "You launched Bolo from its disk image. Drag Bolo to the Applications folder, then launch it from there." with title "Bolo"' >/dev/null 2>&1 || true
        ;;
esac

# --- Login item registration (idempotent, skippable for tests) -------------------

if [ "${BOLO_SKIP_LOGIN_ITEM:-0}" != "1" ]; then
    osascript - "$APP_DIR" >/dev/null 2>&1 <<'APPLESCRIPT' || true
on run argv
  set appPath to item 1 of argv
  tell application "System Events"
    repeat with loginItem in every login item
      try
        if path of loginItem is appPath then delete loginItem
      end try
    end repeat
    make new login item at end of login items with properties {path:appPath, hidden:false}
  end tell
end run
APPLESCRIPT
fi

# --- Self-heal the ad-hoc signature when the bundle was modified -----------------

# Shallow verify is a few milliseconds; --deep on a ~150MB bundle would
# add seconds to every launch. The re-sign path stays --deep.
if ! codesign --verify "$APP_DIR" >/dev/null 2>&1; then
    codesign --force --deep --sign - "$APP_DIR" >> "$LOG" 2>&1 || \
        echo "[bolo] WARNING: could not re-sign the bundle; continuing" >> "$LOG"
fi

# --- Stop stale helper processes from a previous run -----------------------------

pkill -f "$RESOURCES/accessibility_daemon.py" 2>/dev/null || true
pkill -f "$RESOURCES/hotkey.py" 2>/dev/null || true
pkill -f "$RESOURCES/overlay.py" 2>/dev/null || true
pkill -f "$RESOURCES/app_window.py" 2>/dev/null || true

# --- Supervise the runtime --------------------------------------------------------

if [ ! -x "$BIN" ]; then
    echo "[bolo] ERROR: the Bolo runtime binary is missing from the bundle." >> "$LOG"
    osascript -e 'display notification "Bolo is incomplete. Reinstall it from the latest Bolo DMG." with title "Bolo"' >/dev/null 2>&1 || true
    rm -rf "$LOCK_DIR" "$PID_FILE" 2>/dev/null || true
    exit 1
fi

cd "$RESOURCES" || exit 1

export BIN LOG LOCK_DIR PID_FILE RUNTIME_DIR
nohup bash -c '
    cleanup() {
        rm -rf "$LOCK_DIR" "$PID_FILE" /tmp/bolo-instance.lock 2>/dev/null || true
    }
    trap cleanup EXIT INT TERM
    while true; do
        "$BIN" >> "$LOG" 2>&1
        EXIT_CODE=$?
        case $EXIT_CODE in
            0)
                echo "[bolo] clean exit" >> "$LOG"
                break
                ;;
            1)
                echo "[bolo] startup error, not restarting" >> "$LOG"
                break
                ;;
            137|143)
                echo "[bolo] terminated" >> "$LOG"
                rm -rf /tmp/bolo-instance.lock 2>/dev/null || true
                break
                ;;
            *)
                echo "[bolo] exited with code $EXIT_CODE, restarting in 5s" >> "$LOG"
                rm -rf /tmp/bolo-instance.lock 2>/dev/null || true
                sleep 5
                ;;
        esac
    done
' >/dev/null 2>&1 &

SUP_PID=$!
echo "$SUP_PID" > "$PID_FILE"
echo "[bolo] Bolo.app supervisor started (PID $SUP_PID)"
exit 0

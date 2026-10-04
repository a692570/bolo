#!/bin/bash
# Helper inside Bolo.app (Contents/Resources/bolo-helper).
#
# The bundle's main executable (Contents/MacOS/bolo) is a small native
# Mach-O binary (scripts/bundle-launcher.swift) that runs this script and
# stays alive as its parent, so the app keeps a native process identity
# while the runtime runs. This helper keeps the original supervisor
# pattern (lock dir + pid file + relaunch-on-crash under BOLO_RUNTIME_DIR,
# defaulting to /tmp) with paths resolved relative to the bundle instead
# of a source checkout:
#
#   1. pick the matching bundled-python arch dir for this machine
#   2. on first run, create the helper venv from the bundled python and
#      install the bundled wheels fully offline
#   3. export BOLO_BUNDLE_MODE=1 + BOLO_PYTHON so the runtime knows it is
#      running from the bundle and which interpreter to spawn
#   4. refuse a second instance with a clean notification
#   5. register Bolo as a login item (once, via System Events)
#   6. refuse to run a bundle whose code signature does not verify
#   7. supervise the Rust binary, restarting after crashes
#
# BOLO_HELPER_FOREGROUND=1 (set by the native launcher) keeps the
# supervisor in the foreground as a child of the launcher instead of
# detaching with nohup, so the native process stays alive until the
# runtime quits cleanly and the two exit together.
#
# The helper intentionally never builds anything: a source checkout is not
# required, which is the whole point of the DMG.

set -u

# Record quits until startup has installed the owned-lock cleanup trap.
startup_stop_requested=0
trap 'startup_stop_requested=1' TERM INT

# Keep imports from rewriting cache files sealed by the app signature.
export PYTHONDONTWRITEBYTECODE=1

APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
RESOURCES="$APP_DIR/Contents/Resources"
BIN="$APP_DIR/Contents/MacOS/bolo-runtime"
RUNTIME_DIR="${BOLO_RUNTIME_DIR:-/tmp}"
LOG="$RUNTIME_DIR/bolo.log"
LOCK_DIR="$RUNTIME_DIR/bolo-supervisor.lock"
PID_FILE="$RUNTIME_DIR/bolo-supervisor.pid"
VENV_DIR="${BOLO_VENV_DIR:-$HOME/.bolo/bundle-venv}"
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
    # $1: python interpreter to check. $2 (or $BUNDLED_PYTHON_BIN): the
    # bundled runtime bin dir the interpreter must come from. Importing
    # the helper stack is not enough: a venv built from any other
    # interpreter (a source checkout's venv, a stale venv left by an
    # older or moved/copied bundle) would run against the wrong runtime,
    # so the venv's real sys.base_prefix must be the current bundled
    # runtime prefix and those environments are rebuilt offline instead.
    local expected="${2:-${BUNDLED_PYTHON_BIN:-}}"
    "$1" -c 'import objc, AppKit, Foundation, Quartz, ApplicationServices' >/dev/null 2>&1 || return 1
    [ -z "$expected" ] && return 0
    [ "$("$1" -c 'import os, sys; print(os.path.realpath(sys.base_prefix))' 2>/dev/null)" \
        = "$(cd "$expected/.." && pwd -P)" ]
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
    if verify_helpers "$VENV_DIR/bin/python3" "$BUNDLED_PYTHON_BIN"; then VENV_OK=1; else VENV_OK=0; fi
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

# The bundle's venv is its own ($HOME/.bolo/bundle-venv by default), so a
# source install's ~/.bolo/venv is never touched and the two installs stay
# independent. A venv that imports the helper stack AND was created from
# this very bundle's python is a cache and is reused as-is; anything else
# (missing, broken, built from another interpreter, or left behind by a
# DMG-origin/moved/copied bundle) is (re)created from the bundled python +
# wheels, no network required.
if ! [ -x "$VENV_DIR/bin/python3" ] || ! verify_helpers "$VENV_DIR/bin/python3" "$BUNDLED_PYTHON_BIN"; then
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

# --- Refuse to run a corrupted bundle ---------------------------------------------

# Runs before the supervisor lock is taken, so a damaged copy leaves no
# lock behind. (The venv step above may have run first; if this launch
# fails here, the venv stays, which is intentional: it is a user-level
# cache, not per-launch state.) The bundle is never re-signed here:
# modifying a signed bundle breaks its seal, and for a real Developer ID
# release the trust chain must survive from build to launch. If the
# signature no longer verifies, the copy is damaged or tampered with, so
# stop and tell the user to reinstall instead of silently patching it.
# Shallow verify is a few milliseconds; --deep on a ~150MB bundle would
# add seconds to every launch.
if ! codesign --verify "$APP_DIR" >/dev/null 2>&1; then
    echo "[bolo] ERROR: Bolo's code signature does not verify; this copy is damaged or incomplete. Reinstall Bolo from the latest DMG." >> "$LOG" 2>/dev/null || true
    osascript -e 'display notification "This copy of Bolo is damaged or incomplete. Reinstall it from the latest Bolo DMG." with title "Bolo"' >/dev/null 2>&1 || true
    exit 1
fi

# --- Single instance ------------------------------------------------------------

# The lock carries the owner's PID. A second launch that cannot see a
# live owner may take over a stale lock, but it never kills the owner's
# helpers: those pkills run only after this instance owns the lock, so a
# concurrent native launch either observes a live owner and exits, or the
# lock was genuinely stale.
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
# Publish the owner PID immediately, before any further startup work, so
# a concurrent launch of this same bundle sees a live owner and exits
# instead of treating the lock as stale.
echo "$$" > "$PID_FILE"
cleanup_supervisor_lock() {
    if [ -f "$PID_FILE" ] && [ "$(cat "$PID_FILE" 2>/dev/null)" = "${SUPERVISOR_OWNER_PID:-$$}" ]; then
        rm -rf "$LOCK_DIR" "$PID_FILE" 2>/dev/null || true
    fi
}
trap cleanup_supervisor_lock EXIT
if [ "$startup_stop_requested" = 1 ]; then
    exit 0
fi

# --- Friendly nudge when launched straight from the mounted DMG -----------------

case "$APP_DIR" in
    /Volumes/*)
        osascript -e 'display notification "You launched Bolo from its disk image. Drag Bolo to the Applications folder, then launch it from there." with title "Bolo"' >/dev/null 2>&1 || true
        ;;
esac

# --- Login item registration (idempotent, skippable for tests) -------------------

if [ "${BOLO_SKIP_LOGIN_ITEM:-0}" != "1" ]; then
    # Register Bolo as a login item. The launcher detects a login-item
    # startup from the kAEOpenApplication launch AppleEvent
    # (keyAELaunchedAsLogInItem) and stays quiet for those launches, so
    # nothing needs to be passed here; a Finder click still gets the
    # dashboard. BOLO_QUIET_STARTUP=1 remains the explicit quiet override
    # for scripted launches.
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

# --- Stop stale helper processes from a previous run -----------------------------

pkill -f "$RESOURCES/accessibility_daemon.py" 2>/dev/null || true
pkill -f "$RESOURCES/hotkey.py" 2>/dev/null || true
pkill -f "$RESOURCES/overlay.py" 2>/dev/null || true
pkill -f "$RESOURCES/app_window.py" 2>/dev/null || true

# --- Supervise the runtime --------------------------------------------------------

# Past this point the supervisor lock (taken in the single-instance step
# above) is ours: any exit must release it or the next launch reads as
# already running.
if [ ! -x "$BIN" ]; then
    echo "[bolo] ERROR: the Bolo runtime binary is missing from the bundle." >> "$LOG"
    osascript -e 'display notification "Bolo is incomplete. Reinstall it from the latest Bolo DMG." with title "Bolo"' >/dev/null 2>&1 || true
    rm -rf "$LOCK_DIR" "$PID_FILE" 2>/dev/null || true
    exit 1
fi

cd "$RESOURCES" || { rm -rf "$LOCK_DIR" "$PID_FILE" 2>/dev/null || true; exit 1; }

# The instance lock is a global rendezvous the runtime itself uses; only
# the lock owner's cleanup may remove it, otherwise a quitting instance
# could delete a lock a newer owner already re-created.
INSTANCE_LOCK="/tmp/bolo-instance.lock"
own_instance_lock() {
    # Both the supervisor and runtime PID files must name our processes.
    [ -f "$PID_FILE" ] && [ "$(cat "$PID_FILE" 2>/dev/null)" = "${SUPERVISOR_OWNER_PID:-$$}" ] &&
        [ -f "$INSTANCE_LOCK/pid" ] &&
        [ "$(cat "$INSTANCE_LOCK/pid" 2>/dev/null)" = "${RUNTIME_PID:-}" ]
}

# Supervise with an explicit child PID. The runtime is started in the
# background so a termination signal can reach it immediately: the
# synchronous form would delay the trap until the runtime finished on its
# own, leaving a native quit hanging and the runtime orphaned.
start_runtime() {
    "$BIN" >> "$LOG" 2>&1 &
    RUNTIME_PID=$!
}

stop_runtime() {
    if [ -n "${RUNTIME_PID:-}" ] && kill -0 "$RUNTIME_PID" 2>/dev/null; then
        kill "$RUNTIME_PID" 2>/dev/null || true
        wait "$RUNTIME_PID" 2>/dev/null || true
    fi
}

supervise_loop() {
    RUNTIME_PID=""
    shutting_down=0
    on_term() {
        # Forward to the runtime and let the loop see it exit; do not
        # delete shared state from inside a trap.
        shutting_down=1
        stop_runtime
    }
    trap on_term TERM INT
    trap cleanup_supervisor_lock EXIT
    if [ "$startup_stop_requested" = 1 ]; then
        echo "[bolo] terminated during startup" >> "$LOG"
        return
    fi
    while true; do
        start_runtime
        wait "$RUNTIME_PID"
        EXIT_CODE=$?
        if [ "$shutting_down" = 1 ]; then
            echo "[bolo] terminated" >> "$LOG"
            break
        fi
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
                break
                ;;
            *)
                echo "[bolo] exited with code $EXIT_CODE, restarting in 5s" >> "$LOG"
                sleep 5
                ;;
        esac
    done
    # Owned-only cleanup of the global instance lock.
    if own_instance_lock && [ -e "$INSTANCE_LOCK" ]; then
        rm -rf "$INSTANCE_LOCK" 2>/dev/null || true
    fi
}

if [ "${BOLO_HELPER_FOREGROUND:-0}" = "1" ]; then
    # Native-launcher mode: run the supervisor loop as a foreground child
    # so the native main executable stays alive and both processes exit
    # together when the runtime quits cleanly. No nohup, no detach.
    # A Finder launch's open request is written by the launcher itself
    # from the launch AppleEvent; a login-item startup is detected the
    # same way and stays quiet. Nothing more happens here.
    supervise_loop
    exit 0
fi

# Detached mode (no native launcher): same loop, backgrounded.
# Transfer cleanup to the child before the parent exits. Bash keeps $$
# unchanged in a subshell, so ask its child shell for the supervisor PID.
trap - EXIT
(
    SUPERVISOR_OWNER_PID="$(sh -c 'echo "$PPID"')"
    echo "$SUPERVISOR_OWNER_PID" > "$PID_FILE"
    supervise_loop
) &
SUP_PID=$!
echo "[bolo] Bolo.app supervisor started (PID $SUP_PID)"
exit 0

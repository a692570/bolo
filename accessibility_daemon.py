#!/usr/bin/env python3
"""Long-lived accessibility helper for Bolo's Rust runtime.

One persistent process replaces the per-dictation Python spawns: the Rust
runtime used to spawn ``accessibility_trusted.py`` plus ``insert_text.py`` on
every dictation and ``accessibility_context.py`` on the scratch/rewrite paths,
paying interpreter startup plus pyobjc import twice per paste. The daemon
performs the same work in a process that is already warm, cutting the insert
stage to a pipe round-trip.

Protocol: line-delimited JSON on stdin (requests) and stdout (responses),
exactly one response line per request:

* ``{"type":"ping"}`` -> ``{"type":"pong","trusted":<bool>}``
* ``{"type":"trust_check"}`` -> ``{"type":"trust","trusted":<bool>}``
  (``"prompt": true`` also opens the System Settings pane when untrusted)
* ``{"type":"paste","text":...}`` -> ``{"type":"paste_done","ok":<bool>}``
* ``{"type":"select_before_caret","text":...}``
  -> ``{"type":"select_done","selected":<bool>}``
* ``{"type":"read_context"}`` -> ``{"type":"context","app":...,
  "bundle_id":...,"before_cursor":...,"selected_text":...}``, or
  ``{"type":"context","app":null}`` when the read failed outright
* ``{"type":"stop"}`` -> exits 0

Unknown request types answer ``{"type":"error","message":...}`` and the loop
continues; malformed lines are skipped with no response. Stdout carries only
protocol responses, nothing else may print there. The daemon never activates
an application, so it cannot steal the paste target's focus. It exits when
its parent Rust runtime goes away (``BOLO_PARENT_PID``, mirroring hotkey.py)
or when stdin closes.

The ``paste`` reply is sent as soon as Cmd+V is posted; the pasteboard
restore wait (the 350ms window from ``insert_text.py``'s
``BOLO_INSERT_RESTORE_TIMEOUT``) then runs on a background thread so the
dictation critical path does not pay for it. The restore outcome is logged
to stderr as ``[daemon] paste restore restored|external_change|skipped``;
``skipped`` means a newer paste superseded the finalize and the pasteboard
was left untouched. If a newer paste arrives while a finalize is pending,
the pending finalize is cancelled and only the newest paste may restore.
"""

import json
import os
import sys
import threading
import warnings

import accessibility_context
import accessibility_trusted
import insert_text
from objc import ObjCPointerWarning

warnings.filterwarnings("ignore", category=ObjCPointerWarning)

PARENT_PID = int(os.environ.get("BOLO_PARENT_PID") or "0")


def parent_is_alive():
    if PARENT_PID <= 0:
        return True
    try:
        os.kill(PARENT_PID, 0)
    except OSError:
        return False
    return True


def handle_ping(_request):
    return {"type": "pong", "trusted": accessibility_trusted.is_trusted()}


def handle_trust_check(request):
    prompt = bool(request.get("prompt"))
    return {
        "type": "trust",
        "trusted": accessibility_trusted.is_trusted(prompt=prompt),
    }


class PasteRestoreCoordinator:
    """Sequences pastes against the background restores of earlier pastes.

    ``handle_paste`` answers as soon as Cmd+V is posted, so the restore wait
    runs on a worker thread instead of the request loop. The lock makes the
    worker's ownership check plus restore atomic against the next paste's
    snapshot-plus-write, and the generation counter cancels any finalize
    that a newer paste superseded: one pending finalize may act at most, and
    only the newest paste's finalize may restore. A stale finalize must
    never restore across a snapshot it does not own.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0

    def begin(self, text):
        """Run ``insert_text.start_paste`` as one lock-held section.

        Returns ``(generation, state)``; the generation identifies this paste
        to the finalize step. ``state`` is None when the pasteboard refused
        the write, which still cancels any pending finalize: the failed
        attempt cleared the pasteboard, so an older snapshot is stale too.
        """
        with self._lock:
            self._generation += 1
            generation = self._generation
            state = insert_text.start_paste(text)
        return generation, state

    def finalize(self, generation, state):
        """Wait out the restore window, then restore only while this paste
        still owns the pasteboard.

        Returns "skipped" when a newer paste superseded this one, else the
        outcome from ``insert_text.finalize_paste`` ("restored" or
        "external_change").
        """
        with self._lock:
            superseded = generation != self._generation
        if superseded:
            return "skipped"

        def restore_if_still_owned(paste_state):
            # The same lock begin() holds, so a newer paste cannot interleave
            # between this ownership check and the restore write.
            with self._lock:
                if generation != self._generation or not (
                    insert_text.pasteboard_matches_state(paste_state)
                ):
                    return "skipped"
                insert_text.restore_pasteboard(
                    paste_state["pasteboard"], paste_state["snapshot"]
                )
                return "restored"

        return insert_text.finalize_paste(
            state, restore_decider=restore_if_still_owned
        )


PASTE_RESTORE = PasteRestoreCoordinator()


def handle_paste(request):
    text = request.get("text")
    if not isinstance(text, str):
        return {"type": "paste_done", "ok": False}
    if not text:
        # Mirror insert_text.py: an empty payload is a successful no-op.
        return {"type": "paste_done", "ok": True}
    generation, state = PASTE_RESTORE.begin(text)
    if state is None:
        return {"type": "paste_done", "ok": False}
    # Reply the moment Cmd+V is posted; the restore wait would otherwise sit
    # on the dictation critical path for the whole restore window.
    threading.Thread(
        target=_finalize_paste_off_loop,
        args=(generation, state),
        daemon=True,
        name=f"bolo-paste-restore-{generation}",
    ).start()
    return {"type": "paste_done", "ok": True}


def _finalize_paste_off_loop(generation, state):
    """Restore wait for one paste. Logs to stderr only, never stdout."""
    try:
        outcome = PASTE_RESTORE.finalize(generation, state)
    except Exception as error:
        print(f"[daemon] paste restore failed: {error}", file=sys.stderr, flush=True)
        return
    print(f"[daemon] paste restore {outcome}", file=sys.stderr, flush=True)


def handle_select_before_caret(request):
    target = request.get("text")
    if not isinstance(target, str):
        return {"type": "select_done", "selected": False}
    element = accessibility_context.focused_element()
    if element is not None and accessibility_context.is_secure_element(element):
        accessibility_context.log_secure_refusal()
        return {"type": "select_done", "selected": False}
    selected = element is not None and (
        accessibility_context.select_text_immediately_before_caret(element, target)
    )
    return {"type": "select_done", "selected": bool(selected)}


def handle_read_context(_request):
    try:
        app_name, bundle_id = accessibility_context.frontmost_app()
        element = accessibility_context.focused_element()
        secure = element is not None and accessibility_context.is_secure_element(element)
        if secure:
            accessibility_context.log_secure_refusal()
        return {
            "type": "context",
            "app": app_name,
            "bundle_id": bundle_id,
            "before_cursor": ""
            if element is None or secure
            else accessibility_context.text_before_cursor(element),
            "selected_text": ""
            if element is None or secure
            else accessibility_context.selected_text(element),
        }
    except Exception as error:
        # The spawned helper would exit nonzero here, so signal "no context"
        # and let the caller fall back to the per-call path, which fails the
        # same way.
        print(f"[daemon] read_context failed: {error}", file=sys.stderr, flush=True)
        return {"type": "context", "app": None}


HANDLERS = {
    "ping": handle_ping,
    "trust_check": handle_trust_check,
    "paste": handle_paste,
    "select_before_caret": handle_select_before_caret,
    "read_context": handle_read_context,
}


def respond(response):
    sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main() -> int:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        if not parent_is_alive():
            break
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        kind = request.get("type")
        if kind == "stop":
            return 0
        handler = HANDLERS.get(kind)
        if handler is None:
            respond({"type": "error", "message": f"unknown request type: {kind!r}"})
            continue
        try:
            respond(handler(request))
        except Exception as error:
            respond({"type": "error", "message": f"{type(error).__name__}: {error}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

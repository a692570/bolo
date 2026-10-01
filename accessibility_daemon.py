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
"""

import json
import os
import sys
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


def handle_paste(request):
    text = request.get("text")
    if not isinstance(text, str):
        return {"type": "paste_done", "ok": False}
    if not text:
        # Mirror insert_text.py: an empty payload is a successful no-op.
        return {"type": "paste_done", "ok": True}
    return {"type": "paste_done", "ok": insert_text.perform_paste(text) == 0}


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

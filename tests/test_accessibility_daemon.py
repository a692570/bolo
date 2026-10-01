"""Protocol tests for accessibility_daemon.py, Bolo's persistent helper.

Drives the real daemon over its line-delimited JSON protocol using the same
interpreter Bolo launches (BOLO_PYTHON, then ~/.bolo/venv, then the test
interpreter). If none can load pyobjc the module skips instead of failing.

No accessibility permission is needed for the protocol plumbing: ping,
trust_check, read_context, error, malformed-line, and stop paths never mutate
the user's apps. Requests that genuinely mutate the frontmost app (paste,
select_before_caret) only run when BOLO_DAEMON_AX_TESTS=1 is set, because they
perform a real Cmd+V into whatever is focused at the time.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DAEMON = REPO_ROOT / "accessibility_daemon.py"
COLD_START_TIMEOUT = 10.0


def _helper_interpreter():
    """First interpreter that can import Bolo's macOS helper packages."""
    candidates = []
    if os.environ.get("BOLO_PYTHON"):
        candidates.append(Path(os.environ["BOLO_PYTHON"]))
    candidates.append(Path.home() / ".bolo" / "venv" / "bin" / "python3")
    candidates.append(Path(sys.executable))
    for candidate in candidates:
        if not candidate.exists():
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import objc, AppKit, ApplicationServices, Quartz"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if probe.returncode == 0:
            return str(candidate)
    return None


INTERPRETER = _helper_interpreter()

pytestmark = pytest.mark.skipif(
    INTERPRETER is None,
    reason="no interpreter with Bolo's helper packages (pyobjc) is available",
)

AX_GATE = pytest.mark.skipif(
    os.environ.get("BOLO_DAEMON_AX_TESTS") != "1",
    reason=(
        "mutates the frontmost app (real Cmd+V / selection change); "
        "set BOLO_DAEMON_AX_TESTS=1 to opt in"
    ),
)


class DaemonSession:
    """One spawned daemon plus line-level request/response helpers."""

    def __init__(self, parent_pid):
        self.process = subprocess.Popen(
            [INTERPRETER, str(DAEMON)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "BOLO_PARENT_PID": str(parent_pid)},
        )

    def write(self, raw_line):
        self.process.stdin.write(raw_line + "\n")
        self.process.stdin.flush()

    def request(self, payload, timeout=COLD_START_TIMEOUT):
        self.write(json.dumps(payload))
        return self.read_response(timeout)

    def read_response(self, timeout):
        """Read one response line, bounded so a wedged daemon fails fast."""
        import selectors

        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                assert remaining > 0, "daemon produced no response within the timeout"
                if selector.select(remaining):
                    line = self.process.stdout.readline()
                    assert line.strip(), "daemon stdout closed before a response line"
                    return json.loads(line)
        finally:
            selector.close()

    def close(self):
        if self.process.poll() is None:
            try:
                self.write('{"type": "stop"}')
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture
def daemon():
    session = DaemonSession(parent_pid=os.getpid())
    yield session
    session.close()


def test_ping_answers_pong_with_trust_state(daemon):
    reply = daemon.request({"type": "ping"})
    assert reply["type"] == "pong"
    assert isinstance(reply["trusted"], bool)


def test_trust_check_matches_per_call_script(daemon):
    script = subprocess.run(
        [INTERPRETER, str(REPO_ROOT / "accessibility_trusted.py")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert script.returncode == 0
    reply = daemon.request({"type": "trust_check"})
    assert reply["type"] == "trust"
    assert reply["trusted"] == (script.stdout.strip() == "true")


def test_read_context_reports_context_shape(daemon):
    reply = daemon.request({"type": "read_context"})
    assert reply["type"] == "context"
    # "app" is a string on success (possibly empty) and null on a failed read.
    assert reply["app"] is None or isinstance(reply["app"], str)
    if reply["app"] is not None:
        for key in ("bundle_id", "before_cursor", "selected_text"):
            assert isinstance(reply[key], str)


def test_unknown_request_type_answers_error_and_continues(daemon):
    reply = daemon.request({"type": "bogus"})
    assert reply["type"] == "error"
    assert "bogus" in reply["message"]
    assert daemon.request({"type": "ping"})["type"] == "pong"


def test_malformed_lines_are_skipped_without_response(daemon):
    daemon.write("this is not json")
    daemon.write("")
    assert daemon.request({"type": "ping"})["type"] == "pong"


def test_stop_exits_zero(daemon):
    daemon.write('{"type": "stop"}')
    assert daemon.process.wait(timeout=5) == 0


def test_empty_paste_is_a_successful_no_op(daemon):
    reply = daemon.request({"type": "paste", "text": ""})
    assert reply == {"type": "paste_done", "ok": True}


def test_non_string_paste_payload_is_refused(daemon):
    reply = daemon.request({"type": "paste", "text": 7})
    assert reply == {"type": "paste_done", "ok": False}


def test_exits_when_parent_pid_is_dead():
    probe = subprocess.Popen(["/usr/bin/true"])
    dead_pid = probe.pid
    probe.wait()
    del probe
    session = DaemonSession(parent_pid=dead_pid)
    try:
        session.write('{"type": "ping"}')
        # The dead parent is detected when the request arrives: no response
        # line is written, the daemon just exits.
        assert session.process.wait(timeout=10) == 0
    finally:
        session.close()


@AX_GATE
def test_paste_round_trip(daemon):
    reply = daemon.request({"type": "paste", "text": "bolo daemon ax test 9f8e7"})
    assert reply["type"] == "paste_done"
    assert reply["ok"] is True


@AX_GATE
def test_select_before_caret_refuses_a_target_that_is_not_there(daemon):
    reply = daemon.request({"type": "select_before_caret", "text": "zz-not-present-zz"})
    assert reply["type"] == "select_done"
    assert reply["selected"] is False

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

import io
import json
import os
import subprocess
import sys
import threading
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


# ---------------------------------------------------------------------------
# In-process paste tests over the mocked pasteboard from test_insert_text.
#
# The AX-gated tests below prove the real protocol against the real
# pasteboard; these drive the daemon's paste handler in-process with fakes,
# so ordinary test runs prove the reply/finalize split and the cancellation
# guard without posting a real Cmd+V into whatever app is focused.


def _in_process_daemon():
    """The daemon imported into this process; skips without local pyobjc."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        import accessibility_daemon
        import insert_text
    except Exception as error:
        pytest.skip(f"daemon cannot be imported in-process here: {error}")
    return accessibility_daemon, insert_text


def _fresh_daemon(monkeypatch):
    """In-process daemon modules plus a pristine PasteRestoreCoordinator.

    A fresh coordinator per test keeps worker thread names deterministic
    (bolo-paste-restore-1, -2, ...) and the generation counter isolated.
    """
    daemon_module, insert_module = _in_process_daemon()
    monkeypatch.setattr(
        daemon_module, "PASTE_RESTORE", daemon_module.PasteRestoreCoordinator()
    )
    return daemon_module, insert_module


def _restore_workers():
    """Names of the daemon's paste-restore worker threads alive right now."""
    return {
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("bolo-paste-restore-")
    }


def _wait_for_restore_worker(name, timeout=2.5):
    """True once the named paste-restore worker thread has finished."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker = next(
            (thread for thread in threading.enumerate() if thread.name == name),
            None,
        )
        if worker is None:
            return True
        worker.join(max(0.05, deadline - time.monotonic()))
        if not worker.is_alive():
            return True
    return name not in _restore_workers()


def test_paste_reply_is_sent_before_the_restore_window_closes(monkeypatch):
    """The point of the change: the reply must not wait out the restore.

    The restore window is stretched to 1s; the reply must arrive well before
    it while the finalize is provably still pending, and the background
    restore must then put the pre-paste clipboard back without ever writing
    to stdout, which is protocol-only.
    """
    from test_insert_text import install_mock_paste

    daemon_module, _insert = _fresh_daemon(monkeypatch)
    fake, _posted = install_mock_paste(
        monkeypatch, initial_text="user data", restore_timeout=1.0
    )
    stdout_probe = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_probe)

    started = time.monotonic()
    reply = daemon_module.handle_paste({"type": "paste", "text": "dictated words"})
    elapsed = time.monotonic() - started

    assert reply == {"type": "paste_done", "ok": True}
    assert elapsed < 0.5, (
        f"reply took {elapsed:.3f}s; the restore wait still blocks the reply"
    )
    assert fake.current_text() == "dictated words", "finalize should still be pending"

    assert _wait_for_restore_worker("bolo-paste-restore-1")
    assert fake.current_text() == "user data"
    assert stdout_probe.getvalue() == ""


def test_two_quick_pastes_the_first_finalize_never_restores_over_the_second(
    monkeypatch,
):
    """Cancellation guard: the newer paste wins, the stale finalize may not
    restore across the snapshot it no longer owns."""
    from test_insert_text import install_mock_paste

    daemon_module, insert_module = _fresh_daemon(monkeypatch)
    fake, _posted = install_mock_paste(
        monkeypatch, initial_text="user data", restore_timeout=1.0
    )
    real_start_paste = insert_module.start_paste
    states = []

    def spying_start_paste(text):
        state = real_start_paste(text)
        states.append(state)
        return state

    monkeypatch.setattr(insert_module, "start_paste", spying_start_paste)

    assert (
        daemon_module.handle_paste({"type": "paste", "text": "first"})
        == {"type": "paste_done", "ok": True}
    )
    assert (
        daemon_module.handle_paste({"type": "paste", "text": "second"})
        == {"type": "paste_done", "ok": True}
    )

    # Once the first paste's finalize has exited (external change or skipped),
    # the second paste's text must still be installed: the first finalize
    # never restored its snapshot over it.
    assert _wait_for_restore_worker("bolo-paste-restore-1", timeout=1.5)
    assert fake.current_text() == "second"

    # The owning finalize restores the second paste's own pre-paste snapshot.
    assert _wait_for_restore_worker("bolo-paste-restore-2", timeout=2.5)
    assert len(states) == 2
    snapshot_text = next(
        data.decode("utf-8")
        for data_by_type in states[1]["snapshot"]
        for item_type, data in data_by_type.items()
        if item_type == insert_module.NSStringPboardType
    )
    assert fake.current_text() == snapshot_text


def test_stale_finalize_is_skipped_and_the_owning_finalize_restores(monkeypatch):
    """The guard at coordinator level, without thread-scheduling noise."""
    from test_insert_text import install_mock_paste

    daemon_module, _insert = _fresh_daemon(monkeypatch)
    fake, _posted = install_mock_paste(
        monkeypatch, initial_text="user data", restore_timeout=0.05
    )

    generation_one, state_one = daemon_module.PASTE_RESTORE.begin("first")
    generation_two, state_two = daemon_module.PASTE_RESTORE.begin("second")
    assert fake.current_text() == "second"

    # Superseded: no wait, no restore over the newer paste's text.
    assert daemon_module.PASTE_RESTORE.finalize(generation_one, state_one) == "skipped"
    assert fake.current_text() == "second"

    assert daemon_module.PASTE_RESTORE.finalize(generation_two, state_two) == "restored"
    assert fake.current_text() == "first"


def test_failed_pasteboard_write_replies_false_and_cancels_pending_finalize(
    monkeypatch,
):
    from test_insert_text import install_mock_paste

    daemon_module, _insert = _fresh_daemon(monkeypatch)
    fake, _posted = install_mock_paste(
        monkeypatch, initial_text="user data", restore_timeout=0.05
    )

    generation_one, state_one = daemon_module.PASTE_RESTORE.begin("first")
    workers_before = _restore_workers()
    fake.refuse_writes = True
    assert (
        daemon_module.handle_paste({"type": "paste", "text": "dictated"})
        == {"type": "paste_done", "ok": False}
    )
    # No finalize worker was spawned for the refused write...
    time.sleep(0.05)
    assert _restore_workers() == workers_before
    # ...and the failed begin cancelled the pending finalize for paste one.
    assert daemon_module.PASTE_RESTORE.finalize(generation_one, state_one) == "skipped"


@AX_GATE
def test_paste_round_trip(daemon):
    reply = daemon.request({"type": "paste", "text": "bolo daemon ax test 9f8e7"})
    assert reply["type"] == "paste_done"
    assert reply["ok"] is True


@AX_GATE
def test_paste_reply_arrives_before_the_restore_window(monkeypatch):
    """Real-pasteboard proof that the restore wait is off the reply path.

    The restore window is stretched to 1s; the reply must land far sooner,
    which only holds when the finalize runs in the daemon's background.
    """
    monkeypatch.setenv("BOLO_INSERT_RESTORE_TIMEOUT", "1.0")
    session = DaemonSession(parent_pid=os.getpid())
    try:
        started = time.monotonic()
        reply = session.request(
            {"type": "paste", "text": "bolo daemon ax timing 8h7g6"}
        )
        elapsed = time.monotonic() - started
        assert reply == {"type": "paste_done", "ok": True}
        assert elapsed < 0.5, (
            f"paste reply took {elapsed:.3f}s; the restore wait still blocks the reply"
        )
        # Let the daemon's background restore finish before closing stdin,
        # so this test leaves the user's clipboard as it found it.
        time.sleep(1.3)
    finally:
        session.close()


@AX_GATE
def test_select_before_caret_refuses_a_target_that_is_not_there(daemon):
    reply = daemon.request({"type": "select_before_caret", "text": "zz-not-present-zz"})
    assert reply["type"] == "select_done"
    assert reply["selected"] is False

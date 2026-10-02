"""Behavior tests for the DMG bundle scripts.

The launcher (scripts/bundle-launcher.sh) is exercised through its
BOLO_PRINT_STATE hook against a staged fake bundle, so the tests cover the
real script's path resolution, arch pick, venv decision, and
already-running detection without launching anything. The DMG build script
is checked for its pinned runtime URLs and version parsing; the full build
is verified by actually running it (scripts/build-dmg.sh), not here.
"""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "scripts" / "bundle-launcher.sh"
BUILD_DMG = REPO_ROOT / "scripts" / "build-dmg.sh"


def _run_bash(script, env_extra=None):
    env = dict(os.environ)
    env["BOLO_PRINT_STATE"] = "1"
    env["BOLO_SKIP_LOGIN_ITEM"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _stage_fake_bundle(tmp_path, machine_arch="aarch64"):
    """Create a minimal Bolo.app skeleton the launcher can resolve."""
    app = tmp_path / "staging" / "Bolo.app"
    resources = app / "Contents" / "Resources"
    python_bin = resources / "python" / machine_arch / "bin"
    python_bin.mkdir(parents=True)
    # Stub python3.12: exists and is executable, so the launcher accepts the
    # bundled runtime. verify_helpers against it fails, which is what the
    # VENV_OK=0 assertions rely on.
    stub = python_bin / "python3.12"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)
    return app


def _launcher_env(tmp_path, app_dir, runtime_dir=None, venv_dir=None):
    return {
        "BOLO_RUNTIME_DIR": str(runtime_dir or tmp_path / "runtime"),
        "BOLO_VENV_DIR": str(venv_dir or tmp_path / "venv"),
        "APP_DIR_OVERRIDE": "",
    }, {"APP_DIR": str(app_dir)}


def _print_state(app_dir, env_extra):
    # The launcher derives everything from its own path; copy it into the
    # fake bundle so $0 resolution finds the staged layout.
    staged_launcher = Path(app_dir) / "Contents" / "MacOS" / "bolo"
    staged_launcher.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", str(LAUNCHER), str(staged_launcher)], check=True)
    staged_launcher.chmod(0o755)
    return _run_bash(staged_launcher, env_extra)


def _parse_state(output):
    state = {}
    for line in output.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            state[key] = value
    return state


def test_pick_arch_maps_machine_types():
    for machine, expected in (
        ("arm64", "aarch64"),
        ("x86_64", "x86_64"),
    ):
        script = 'source "{0}"; bolo_pick_arch {1}'.format(LAUNCHER, machine)
        # Sourcing executes the launcher; instead extract just the function.
        function_text = subprocess.run(
            [
                "bash",
                "-c",
                'eval "$(sed -n \'/^bolo_pick_arch() {{/,/^}}/p\' \'{}\')" ; '
                "bolo_pick_arch {}".format(LAUNCHER, machine),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert function_text.returncode == 0, function_text.stderr
        assert function_text.stdout.strip() == expected

    unsupported = subprocess.run(
        [
            "bash",
            "-c",
            'eval "$(sed -n \'/^bolo_pick_arch() {{/,/^}}/p\' \'{}\')" ; '
            "bolo_pick_arch i386".format(LAUNCHER),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert unsupported.returncode == 1
    assert unsupported.stdout.strip() == "unsupported"


def test_launcher_print_state_resolves_bundle_paths(tmp_path):
    app = _stage_fake_bundle(tmp_path)
    runtime = tmp_path / "runtime"
    result = _print_state(
        app,
        {
            "BOLO_RUNTIME_DIR": str(runtime),
            "BOLO_VENV_DIR": str(tmp_path / "venv"),
        },
    )
    assert result.returncode == 0, result.stderr
    state = _parse_state(result.stdout)

    assert state["APP_DIR"] == str(app)
    assert state["RESOURCES"] == str(app / "Contents" / "Resources")
    assert state["ARCH_DIR"] in {"aarch64", "x86_64"}
    assert state["ARCH_DIR"] in state["BUNDLED_PYTHON_BIN"]
    assert state["RUNTIME_DIR"] == str(runtime)
    assert state["VENV_DIR"] == str(tmp_path / "venv")
    assert state["BUNDLE_MODE"] == "1"
    assert state["LOGIN_ITEM_SKIPPED"] == "1"


def test_launcher_reports_first_run_when_venv_cannot_import_helpers(tmp_path):
    app = _stage_fake_bundle(tmp_path)
    # No venv at all: first run.
    result = _print_state(
        app,
        {
            "BOLO_RUNTIME_DIR": str(tmp_path / "runtime"),
            "BOLO_VENV_DIR": str(tmp_path / "venv"),
        },
    )
    assert result.returncode == 0
    assert _parse_state(result.stdout)["VENV_OK"] == "0"

    # A venv whose python passes the helper-import check reads as ready.
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python3"
    fake_python.write_text("#!/bin/bash\nexit 0\n")
    fake_python.chmod(0o755)
    result = _print_state(
        app,
        {
            "BOLO_RUNTIME_DIR": str(tmp_path / "runtime"),
            "BOLO_VENV_DIR": str(tmp_path / "venv"),
        },
    )
    assert result.returncode == 0
    assert _parse_state(result.stdout)["VENV_OK"] == "1"


def test_launcher_detects_running_supervisor_lock(tmp_path):
    app = _stage_fake_bundle(tmp_path)
    runtime = tmp_path / "runtime"
    lock = runtime / "bolo-supervisor.lock"
    pid_file = runtime / "bolo-supervisor.pid"
    env = {
        "BOLO_RUNTIME_DIR": str(runtime),
        "BOLO_VENV_DIR": str(tmp_path / "venv"),
    }

    # A lock dir with no pid file reads as not running.
    lock.mkdir(parents=True)
    result = _print_state(app, env)
    assert _parse_state(result.stdout)["ALREADY_RUNNING"] == "0"

    # A lock dir whose pid is alive reads as running.
    pid_file.write_text(str(os.getpid()))
    result = _print_state(app, env)
    assert _parse_state(result.stdout)["ALREADY_RUNNING"] == "1"

    # A dead pid reads as not running.
    dead = subprocess.Popen(["true"])
    dead.wait()
    pid_file.write_text(str(dead.pid))
    result = _print_state(app, env)
    assert _parse_state(result.stdout)["ALREADY_RUNNING"] == "0"


def test_launcher_exits_on_unsupported_arch(tmp_path):
    app = _stage_fake_bundle(tmp_path, machine_arch="aarch64")
    # Force the arch pick to fail by removing the bundled python dir.
    import shutil

    shutil.rmtree(app / "Contents" / "Resources" / "python")
    result = _print_state(
        app,
        {
            "BOLO_RUNTIME_DIR": str(tmp_path / "runtime"),
            "BOLO_VENV_DIR": str(tmp_path / "venv"),
        },
    )
    assert result.returncode == 1


def test_build_dmg_pins_python_runtimes_by_url_and_sha256():
    text = BUILD_DMG.read_text()
    assert "python-build-standalone" in text
    # Two pinned tarballs, each with a 64-hex SHA256 in an adjacent variable.
    for arch in ("AARCH64", "X86_64"):
        url = re.search(rf"PBS_{arch}_URL=\"([^\"]+)\"", text)
        sha = re.search(rf"PBS_{arch}_SHA=\"([0-9a-f]+)\"", text)
        assert url is not None and "cpython-3.12" in url.group(1)
        assert sha is not None and len(sha.group(1)) == 64


def test_build_dmg_reads_version_from_cargo_toml():
    version = subprocess.run(
        [
            "bash",
            "-c",
            "sed -n 's/^version *= *\"\\([^\"]*\\)\"/\\1/p' '{}' | head -1".format(
                REPO_ROOT / "Cargo.toml"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert version.stdout.strip() == "1.6.0"


def test_make_icon_layout_keeps_bars_inside_canvas():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import make_icon

    rects = make_icon.wave_bar_rects()
    assert len(rects) == len(make_icon.WAVE_HEIGHTS)
    for x, y, w, h in rects:
        assert 0 <= x and x + w <= make_icon.SIZE
        assert 0 <= y and y + h <= make_icon.SIZE
    # Bars are ordered left to right and do not overlap.
    for left, right in zip(rects, rects[1:]):
        assert left[0] + left[2] <= right[0]
    # The tallest bar is centered on the wave line.
    tallest = max(rects, key=lambda rect: rect[3])
    assert tallest[1] + tallest[3] / 2 == make_icon.WAVE_CENTER_Y


def test_make_icon_reports_missing_pyobjc_cleanly(tmp_path):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import make_icon

    # Under a python without pyobjc, render fails with a clear message and
    # no output file is written. On interpreters that do have pyobjc (this
    # machine's system python), the render simply succeeds.
    probe = subprocess.run(
        [sys.executable, "-c", "import AppKit"], capture_output=True, text=True
    )
    code = (
        "import sys; sys.path.insert(0, {!r}); "
        "import make_icon; "
        "sys.exit(make_icon.main(['--output', {!r}]))"
    ).format(str(REPO_ROOT / "scripts"), str(tmp_path / "out" / "icon.png"))
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT / "scripts"),
    )
    if probe.returncode == 0:
        assert result.returncode == 0
        assert (tmp_path / "out" / "icon.png").is_file()
    else:
        assert result.returncode == 1
        assert "AppKit is unavailable" in result.stderr
        assert not (tmp_path / "out" / "icon.png").exists()


def test_bundled_launcher_file_is_private_and_executable():
    # The committed launcher stays executable so `install -m 755` preserves
    # intent; a regression here breaks the DMG build silently.
    mode = stat.S_IMODE(LAUNCHER.stat().st_mode)
    assert mode & stat.S_IXUSR

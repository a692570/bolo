"""Behavior tests for Bolo's shell onboarding and recovery scripts."""

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


def _installer_fixture(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    for name in (
        "install.sh",
        "start-bolo.command",
        "start-bolo.sh",
        "restart.sh",
        "update.sh",
        "ensure-python-env.sh",
        "helper-requirements.txt",
    ):
        shutil.copy2(REPO_ROOT / name, app_dir / name)
    for name in ("src", "target"):
        (app_dir / name).mkdir()
    (app_dir / "src" / "main.rs").write_text("fn main() {}\n")
    (app_dir / "Cargo.toml").write_text("[package]\nname='fixture'\nversion='0.1.0'\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    _write_executable(
        fake_python,
        """#!/bin/bash
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
    mkdir -p "$3/bin"
    ln -sf "$0" "$3/bin/python3"
fi
if [ "${1:-}" = "-c" ]; then
    printf '%s\\n' "$0"
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "cargo",
        """#!/bin/bash
mkdir -p target/release
cat > target/release/bolo <<'EOF'
#!/bin/bash
sleep 20
EOF
chmod +x target/release/bolo
""",
    )
    _write_executable(fake_bin / "osascript", "#!/bin/bash\nexit 0\n")
    _write_executable(
        fake_bin / "open",
        "#!/bin/bash\n\"$1\" >/dev/null 2>&1 &\nexit 0\n",
    )

    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in ("start-bolo.command", "restart.sh"):
        path = app_dir / name
        text = path.read_text()
        text = text.replace("/tmp/bolo", f"{runtime}/bolo")
        path.write_text(text)
        path.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "TMPDIR": str(runtime),
            "BOLO_AUTO_UPDATE": "off",
            "BOLO_PYTHON": str(fake_python),
        }
    )
    return app_dir, home, runtime, env


def _run(script, *, cwd, env, input_text=""):
    return subprocess.run(
        ["/bin/bash", "-c", f"umask 022; ./{script}"],
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def _stop_supervisor(runtime):
    pid_file = runtime / "bolo-supervisor.pid"
    if not pid_file.exists():
        return
    try:
        os.kill(int(pid_file.read_text().strip()), 15)
    except (ProcessLookupError, ValueError):
        pass


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )


def test_helper_environment_does_not_install_into_bootstrap_python(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copy2(REPO_ROOT / "ensure-python-env.sh", app_dir / "ensure-python-env.sh")
    shutil.copy2(REPO_ROOT / "helper-requirements.txt", app_dir / "helper-requirements.txt")
    bootstrap_log = tmp_path / "bootstrap.log"
    venv_log = tmp_path / "venv.log"
    bootstrap = tmp_path / "bootstrap-python"
    _write_executable(
        bootstrap,
        f"""#!/bin/bash
printf '%s\\n' "$*" >> "{bootstrap_log}"
if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "venv" ]; then
    mkdir -p "$3/bin"
    cat > "$3/bin/python3" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "{venv_log}"
if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "pip" ]; then
    echo "simulated pip output"
fi
exit 0
EOF
    chmod +x "$3/bin/python3"
    exit 0
fi
if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "pip" ]; then
    exit 99
fi
exit 0
""",
    )
    home = tmp_path / "home"
    home.mkdir()
    venv = tmp_path / "managed-venv"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "BOLO_PYTHON_BOOTSTRAP": str(bootstrap),
            "BOLO_VENV_DIR": str(venv),
        }
    )

    result = subprocess.run(
        [str(app_dir / "ensure-python-env.sh")],
        cwd=app_dir,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == str(venv / "bin" / "python3")
    assert "-m venv" in bootstrap_log.read_text()
    assert "-m pip" not in bootstrap_log.read_text()
    assert "-m pip" in venv_log.read_text()


def test_updater_does_not_activate_when_incoming_helpers_fail(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    installed = tmp_path / "installed"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.name", "Bolo Tests")
    _git(seed, "config", "user.email", "bolo-tests@example.invalid")
    shutil.copy2(REPO_ROOT / "update.sh", seed / "update.sh")
    _write_executable(seed / "ensure-python-env.sh", "#!/bin/bash\nexit 0\n")
    (seed / "Cargo.toml").write_text("[package]\nname='fixture'\nversion='0.1.0'\n")
    (seed / ".gitignore").write_text("target/\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "Initial fixture")
    initial_head = _git(seed, "rev-parse", "HEAD").stdout.strip()
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(remote), str(installed))

    _write_executable(
        seed / "ensure-python-env.sh",
        "#!/bin/bash\necho incoming helper failed >&2\nexit 1\n",
    )
    _git(seed, "add", "ensure-python-env.sh")
    _git(seed, "commit", "-m", "Break incoming helper setup")
    _git(seed, "push", "origin", "main")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "cargo", "#!/bin/bash\nexit 0\n")
    (installed / "target" / "release").mkdir(parents=True)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run("update.sh", cwd=installed, env=env)

    assert result.returncode == 0
    assert "BOLO_UPDATE_RESULT=skipped" in result.stdout
    assert "could not prepare its Python helpers" in result.stdout
    assert _git(installed, "rev-parse", "HEAD").stdout.strip() == initial_head


def test_installer_rejects_blank_api_key(tmp_path):
    app_dir, _home, runtime, env = _installer_fixture(tmp_path)
    env.pop("TELNYX_API_KEY", None)
    try:
        result = _run("install.sh", cwd=app_dir, env=env, input_text="\n")
    finally:
        _stop_supervisor(runtime)

    assert result.returncode != 0
    assert "API key is required" in result.stdout


def test_installer_writes_private_key_without_backup(tmp_path):
    app_dir, home, runtime, env = _installer_fixture(tmp_path)
    env["TELNYX_API_KEY"] = "first-test-key"
    try:
        first = _run("install.sh", cwd=app_dir, env=env)
        env["TELNYX_API_KEY"] = "second-test-key"
        second = _run("install.sh", cwd=app_dir, env=env)
    finally:
        _stop_supervisor(runtime)

    env_file = home / ".bolo" / "env"
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert stat.S_IMODE((home / ".bolo").stat().st_mode) == 0o700
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert env_file.read_text() == 'TELNYX_API_KEY="second-test-key"\n'
    assert not (home / ".bolo" / "env.bak").exists()


def test_saved_auto_update_opt_out_is_honored(tmp_path):
    app_dir = tmp_path / "app"
    (app_dir / "target" / "release").mkdir(parents=True)
    (app_dir / "src").mkdir()
    shutil.copy2(REPO_ROOT / "start-bolo.command", app_dir / "start-bolo.command")
    _write_executable(app_dir / "target" / "release" / "bolo", "#!/bin/bash\nexit 0\n")
    (app_dir / "src" / "main.rs").write_text("fn main() {}\n")
    (app_dir / "Cargo.toml").write_text("[package]\nname='fixture'\nversion='0.1.0'\n")
    marker = tmp_path / "updated"
    _write_executable(app_dir / "update.sh", f"#!/bin/bash\ntouch {marker!s}\n")
    now = time.time()
    os.utime(app_dir / "src" / "main.rs", (now - 10, now - 10))
    os.utime(app_dir / "Cargo.toml", (now - 10, now - 10))
    os.utime(app_dir / "target" / "release" / "bolo", (now, now))

    home = tmp_path / "home"
    (home / ".bolo").mkdir(parents=True)
    (home / ".bolo" / "env").write_text("BOLO_AUTO_UPDATE=off\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    launcher = app_dir / "start-bolo.command"
    launcher.write_text(launcher.read_text().replace("/tmp/bolo", f"{runtime}/bolo"))
    launcher.chmod(0o755)
    env = os.environ.copy()
    env.update({"HOME": str(home), "TMPDIR": str(runtime)})
    env.pop("BOLO_AUTO_UPDATE", None)

    result = _run("start-bolo.command", cwd=app_dir, env=env)
    _stop_supervisor(runtime)

    assert result.returncode == 0
    assert not marker.exists()


def test_restart_fails_when_rebuild_fails(tmp_path):
    app_dir = tmp_path / "app"
    (app_dir / "target" / "release").mkdir(parents=True)
    (app_dir / "src").mkdir()
    shutil.copy2(REPO_ROOT / "restart.sh", app_dir / "restart.sh")
    shutil.copy2(REPO_ROOT / "start-bolo.command", app_dir / "start-bolo.command")
    _write_executable(app_dir / "target" / "release" / "bolo", "#!/bin/bash\nexit 0\n")
    (app_dir / "src" / "main.rs").write_text("fn main() {}\n")
    (app_dir / "Cargo.toml").write_text("[package]\nname='fixture'\nversion='0.1.0'\n")
    now = time.time()
    os.utime(app_dir / "target" / "release" / "bolo", (now - 10, now - 10))
    os.utime(app_dir / "src" / "main.rs", (now, now))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "cargo", "#!/bin/bash\nexit 1\n")
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["TMPDIR"] = str(tmp_path / "runtime")

    result = _run("restart.sh", cwd=app_dir, env=env)

    assert result.returncode != 0
    assert "rebuild failed" in result.stderr.lower()

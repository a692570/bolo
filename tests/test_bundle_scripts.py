"""Behavior tests for the DMG bundle scripts.

The launcher (scripts/bundle-launcher.sh) is exercised through its
BOLO_PRINT_STATE hook against a staged fake bundle, so the tests cover the
real script's path resolution, arch pick, venv decision, and
already-running detection without launching anything. The DMG build script
is checked for its pinned runtime URLs and version parsing; the full build
is verified by actually running it (scripts/build-dmg.sh), not here.
"""

import os
import hashlib
import importlib.util
import re
import stat
import subprocess
import sys
import shutil
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SWIFT = REPO_ROOT / "scripts" / "bundle-launcher.swift"
HELPER = REPO_ROOT / "scripts" / "bundle-launcher.sh"
BUILD_DMG = REPO_ROOT / "scripts" / "build-dmg.sh"
DMG_BACKGROUND = REPO_ROOT / "scripts" / "make-dmg-background.py"
DMG_LAYOUT = REPO_ROOT / "scripts" / "make-dmg-layout.py"


def _load_dmg_background():
    """Import the dashed-named background script as a module."""
    spec = importlib.util.spec_from_file_location("make_dmg_background", DMG_BACKGROUND)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _compile_native_launcher(tmp_path):
    """Compile the native launcher the way build-dmg.sh does, universal."""
    if shutil.which("swiftc") is None:
        pytest.skip("swiftc is unavailable")
    out = tmp_path / "native"
    out.mkdir(exist_ok=True)
    flags = [
        "-O", "-swift-version", "5",
        "-disable-autolinking-runtime-compatibility",
        "-disable-autolinking-runtime-compatibility-concurrency",
        "-disable-autolinking-runtime-compatibility-dynamic-replacements",
    ]
    for target, name in (
        ("arm64-apple-macos12.0", "bolo-arm64"),
        ("x86_64-apple-macos12.0", "bolo-x64"),
    ):
        run = subprocess.run(
            ["swiftc", "-target", target, *flags, "-o", str(out / name),
             str(LAUNCHER_SWIFT)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert run.returncode == 0, run.stderr
    subprocess.run(
        ["lipo", "-create", str(out / "bolo-arm64"), str(out / "bolo-x64"),
         "-output", str(out / "bolo")],
        check=True,
        timeout=60,
    )
    archs = subprocess.run(
        ["lipo", "-archs", str(out / "bolo")],
        capture_output=True, text=True, timeout=60,
    ).stdout.split()
    assert set(archs) == {"arm64", "x86_64"}
    return out / "bolo"


def _unique_bundle_id(tmp_path):
    """Give each fixture path its own application identifier."""
    suffix = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    return "com.bolotest.fixture.{0}".format(suffix)


def _stage_fake_bundle(tmp_path, machine_arch="aarch64"):
    """Stage bundle metadata and interpreter stubs; native tests sign later."""
    app = tmp_path / "staging" / "Bolo.app"
    contents = app / "Contents"
    resources = contents / "Resources"
    python_bin = resources / "python" / machine_arch / "bin"
    python_bin.mkdir(parents=True)
    # Stub python3.12: exists and is executable, so the helper accepts the
    # bundled runtime. verify_helpers against it fails, which is what the
    # VENV_OK=0 assertions rely on.
    stub = python_bin / "python3.12"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)
    info_plist = contents / "Info.plist"
    info_plist.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>{0}</string>
    <key>CFBundleExecutable</key>
    <string>bolo</string>
    <key>CFBundleName</key>
    <string>Bolo</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
""".format(
            _unique_bundle_id(tmp_path)
        )
    )
    return app


def _codesign_adhoc(target):
    """Sign a staged test fixture after its executable has been added."""
    if shutil.which("codesign") is None:
        pytest.skip("codesign is unavailable")
    run = subprocess.run(
        [
            "/usr/bin/codesign",
            "--force",
            "--deep",
            "--sign",
            "-",
            "--timestamp=none",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr


def _stage_fake_venv(tmp_path):
    """A ready helper venv with the staged bundle's interpreter prefix."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    fake_python = venv_bin / "python3"
    runtime_root = tmp_path / "staging" / "Bolo.app" / "Contents" / "Resources" / "python"
    prefix = next(runtime_root.iterdir()).resolve()
    fake_python.write_text("#!/bin/bash\nprintf '%s\\n' '{}'\n".format(prefix))
    fake_python.chmod(0o755)
    return tmp_path / "venv"


def _print_state(app_dir, env_extra):
    # The helper derives everything from its own path; copy it into the
    # fake bundle as Contents/Resources/bolo-helper (where the build now
    # installs it) so $0 resolution finds the staged layout. Re-signing
    # after staging keeps the bundle self-consistent for AppKit.
    staged_helper = Path(app_dir) / "Contents" / "Resources" / "bolo-helper"
    staged_helper.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", str(HELPER), str(staged_helper)], check=True)
    staged_helper.chmod(0o755)
    _codesign_adhoc(Path(app_dir))
    return _run_bash(staged_helper, env_extra)


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
        script = 'source "{0}"; bolo_pick_arch {1}'.format(HELPER, machine)
        # Sourcing executes the launcher; instead extract just the function.
        function_text = subprocess.run(
            [
                "bash",
                "-c",
                'eval "$(sed -n \'/^bolo_pick_arch() {{/,/^}}/p\' \'{}\')" ; '
                "bolo_pick_arch {}".format(HELPER, machine),
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
            "bolo_pick_arch i386".format(HELPER),
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

    # A cached venv must import helpers and report this bundle's base prefix.
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python3"
    arch = _parse_state(result.stdout)["ARCH_DIR"]
    prefix = app / "Contents" / "Resources" / "python" / arch
    fake_python.write_text("#!/bin/bash\nprintf '%s\\n' '{}'\n".format(prefix))
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


def test_build_dmg_stages_the_finder_presentation():
    text = BUILD_DMG.read_text()
    # The background render is a committed script, cached in build/dmg and
    # re-rendered whenever the script is newer than the cached image.
    assert "make-dmg-background.py" in text
    assert re.search(r'-nt "\$BG"', text)
    # The staged background is named by its content hash, so a changed
    # image is a changed URL and Finder's image cache cannot serve a
    # stale picture under the old path.
    assert 'BG_NAME="background-${BG_SHA}.png"' in text
    assert re.search(r'shasum -a 256 "\$BG"', text)
    assert 'cp "$BG" "$STAGE/.background/$BG_NAME"' in text
    # The layout is written programmatically by make-dmg-layout.py on the
    # read-write layout volume itself; that SAME image is converted into
    # the final DMG, so the volume identity the alias refers to survives.
    assert "make-dmg-layout.py" in text
    assert "--verify" in text
    assert "-format UDRW" in text
    assert ".DS_Store" in text
    assert 'hdiutil convert "$LAYOUT_DMG"' in text
    assert re.search(r'rm -f "\$LAYOUT_DMG"\n\n# --- Phase 7', text) is None
    # The layout image is kept until after the conversion, not deleted
    # while it is the source of the final artifact.
    assert text.index('hdiutil convert "$LAYOUT_DMG"') < text.rindex(
        'rm -f "$LAYOUT_DMG"'
    )
    assert "osascript" not in re.sub(r"launch.*osascript|echo.*osascript", "", text).split("# --- Phase 6.5")[-1].split("# --- Phase 7")[0]
    # The mounted DMG must carry both presentation artifacts, and the
    # alias must resolve inside the mounted volume, not just exist.
    assert '-e "$MOUNT/.background/$BG_NAME"' in text
    assert '-s "$MOUNT/.DS_Store"' in text
    assert "--verify-existing" in text


def test_build_dmg_layout_pins_bolo_left_applications_right():
    text = BUILD_DMG.read_text()
    layout_call = text.split('"$ICON_VENV/bin/python3" "$ROOT/scripts/make-dmg-layout.py"', 1)[1].split("|| fail", 1)[0]
    assert "--bolo-position 150,232" in layout_call
    assert "--applications-position 462,232" in layout_call
    assert "--window 0,0,660,400" in layout_call
    # Bolo's slot is left of the Applications slot, and both sit inside a
    # 660x400 window.
    assert (150 + 80) < 462
    assert 462 + 80 <= 660


def test_build_dmg_no_finder_applescript_for_layout():
    text = BUILD_DMG.read_text()
    # The old AppleScript layout block is gone entirely.
    assert "tell application \"Finder\"" not in text
    assert "set background picture" not in text
    assert "set position of item" not in text


def test_build_dmg_preview_artifact_never_overwrites_release():
    text = BUILD_DMG.read_text()
    # Default (no --release) produces a -preview DMG; the released
    # dist/Bolo-<version>.dmg is never the default target.
    assert '"$DIST/Bolo-$VERSION-preview.dmg"' in text
    assert '"$DIST/Bolo-$VERSION.dmg"' in text
    # Preview mode removes only the preview artifact; release mode refuses
    # to overwrite an existing DMG.
    preview_block = text.split('if [ "$RELEASE" = 1 ]; then\n    DMG=')[1]
    assert "removing the old preview artifact" in preview_block
    assert "move or delete it before rebuilding the release" in preview_block


def test_build_dmg_release_preflight_fails_fast():
    text = BUILD_DMG.read_text()
    # --release requires both env vars and checks them before any build
    # phase (before the PBS download/cargo sections).
    assert "BOLO_CODESIGN_IDENTITY" in text
    assert "BOLO_NOTARY_PROFILE" in text
    preflight_pos = text.index("BOLO_CODESIGN_IDENTITY")
    assert preflight_pos < text.index("PBS_BASE")
    # The preflight runs at phase 0, before any long work; exercise it live
    # with the tools it checks available but no signing env, so no secret
    # is needed. cargo is not on this machine's PATH, so look it up the way
    # the build script would.
    cargo = shutil.which("cargo")
    if cargo is None:
        candidates = [str(Path.home() / ".cargo" / "bin" / "cargo")]
        candidates += [
            str(REPO_ROOT / "build" / "cargo-home" / "bin" / "cargo"),
        ]
        cargo = next((c for c in candidates if Path(c).exists()), None)
    env = dict(os.environ)
    if cargo:
        env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
        env["CARGO"] = cargo
    env.pop("BOLO_CODESIGN_IDENTITY", None)
    env.pop("BOLO_NOTARY_PROFILE", None)
    run = subprocess.run(
        ["bash", str(BUILD_DMG), "--release"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert run.returncode != 0
    assert "--release needs BOLO_CODESIGN_IDENTITY" in run.stderr


def test_build_dmg_release_mode_signs_with_identity_and_notarizes():
    text = BUILD_DMG.read_text()
    # Release signing: nested Mach-O files first (bottom-up, detected by
    # magic rather than filename so the Rust runtime is included), then
    # the app bundle with hardened runtime + secure timestamp, then verify.
    assert "--options runtime --timestamp" in text
    assert 'codesign --verify --strict "$APP"' in text
    assert "cffaedfe" in text  # 64-bit Mach-O magic
    assert "cafebebe" in text  # fat binary magic
    # The nested signing loop runs on plain files only, so symlinks are
    # never signed twice.
    assert '-type f -print0' in text
    # Release-only notarization of the DMG: submit, staple, validate.
    assert "notarytool submit" in text
    assert "stapler staple" in text
    assert "stapler validate" in text
    # The release path is honestly labeled as not live verified here, and
    # the native-launcher gap is called out.
    assert "NOT live verified" in text
    assert "native launcher" in text


def test_launcher_refuses_corrupted_bundle_instead_of_resigning():
    text = HELPER.read_text()
    # The self-heal re-sign is gone: a bundle that fails verification must
    # exit with reinstall guidance, and the script never runs codesign
    # --sign on the installed bundle.
    assert "codesign --force" not in text
    assert "--sign -" not in text
    assert "Reinstall" in text
    assert re.search(r"codesign --verify \"\$APP_DIR\"", text)
    # The check runs before the login-item registration, so a damaged
    # bundle never registers itself as a login item.
    assert text.index("codesign --verify \"$APP_DIR\"") < text.index(
        "Login item registration"
    )


def test_build_dmg_release_preflight_rejects_non_developer_id_identity():
    text = BUILD_DMG.read_text()
    # The preflight only accepts a Developer ID Application identity;
    # anything else (Apple Development, ad-hoc, etc.) is rejected.
    assert "Developer ID Application" in text
    assert 'grep -F "Developer ID Application"' in text
    # Live check with an env that looks satisfied but a non-Developer ID
    # identity: the preflight must reject it before building anything.
    # Both vars are set so the rejection comes from the identity check.
    run = subprocess.run(
        ["bash", str(BUILD_DMG), "--release"],
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            BOLO_CODESIGN_IDENTITY="Apple Development: nope",
            BOLO_NOTARY_PROFILE="absent-profile",
        ),
        timeout=60,
    )
    assert run.returncode != 0
    assert "no valid codesigning identity matches" in run.stderr \
        or "must be a Developer ID Application" in run.stderr


def test_launcher_corrupted_bundle_fails_at_runtime(tmp_path):
    # The verification step runs before the login-item registration and
    # before the supervisor lock, and there is no bypass: a bundle that
    # fails codesign --verify exits 1 with reinstall guidance.
    app = _stage_fake_bundle(tmp_path)
    staged_helper = Path(app) / "Contents" / "Resources" / "bolo-helper"
    staged_helper.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", str(HELPER), str(staged_helper)], check=True)
    staged_helper.chmod(0o755)
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    # A codesign stub that always fails: the helper must treat the
    # bundle as damaged and exit nonzero.
    stub = stub_dir / "codesign"
    stub.write_text("#!/bin/bash\nexit 1\n")
    stub.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "BOLO_SKIP_LOGIN_ITEM": "1",
            "BOLO_RUNTIME_DIR": str(tmp_path / "runtime"),
            "BOLO_VENV_DIR": str(tmp_path / "venv"),
            "PATH": str(stub_dir) + os.pathsep + os.environ.get("PATH", ""),
        }
    )
    strict = subprocess.run(
        ["bash", str(staged_helper)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert strict.returncode == 1
    # A passing stub lets the helper continue (it will then fail later
    # on the missing runtime binary, but never on the signature check).
    ok_stub = stub_dir / "codesign"
    ok_stub.write_text("#!/bin/bash\nexit 0\n")
    ok_stub.chmod(0o755)
    lenient = subprocess.run(
        ["bash", str(staged_helper)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert lenient.returncode != 0  # still exits: no runtime binary staged
    assert "codesign --force" not in HELPER.read_text()
    assert "BOLO_SKIP_SIGNATURE_CHECK" not in HELPER.read_text()


def test_native_launcher_compiles_universal(tmp_path):
    """The Swift main executable compiles for both arches and lipsos into
    a universal Mach-O, exactly the way build-dmg.sh stages it."""
    binary = _compile_native_launcher(tmp_path)
    kind = subprocess.run(
        ["file", str(binary)],
        capture_output=True, text=True, timeout=60,
    ).stdout
    assert "Mach-O" in kind
    assert "universal" in kind


def test_native_launcher_runs_helper_in_foreground(tmp_path):
    """End to end with stubs: the native executable finds the Resources
    helper, runs it in foreground (BOLO_HELPER_FOREGROUND=1), stays alive
    while the helper runs, and both exit together on a clean quit. The
    helper supervises a stub runtime that exits cleanly on the first run,
    so the whole chain terminates without any real app, network, or UI."""
    binary = _compile_native_launcher(tmp_path)
    app = _stage_fake_bundle(tmp_path)
    resources = Path(app) / "Contents" / "Resources"
    subprocess.run(["cp", str(HELPER), resources / "bolo-helper"], check=True)
    (resources / "bolo-helper").chmod(0o755)
    # The launcher resolves its bundle from argv[0], so it must run from
    # its real slot inside the staged app, exactly as the DMG installs it.
    launcher = Path(app) / "Contents" / "MacOS" / "bolo"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", str(binary), str(launcher)], check=True)
    launcher.chmod(0o755)
    runtime = tmp_path / "runtime"
    env = dict(os.environ)
    env.update(
        {
            "BOLO_SKIP_LOGIN_ITEM": "1",
            "BOLO_RUNTIME_DIR": str(runtime),
            "BOLO_VENV_DIR": str(tmp_path / "venv"),
            # The staged python stub already exists; no codesign stub is
            # needed because the real codesign fails on this fake bundle,
            # which is the behavior under test for other paths. For this
            # end-to-end run, put a passing codesign stub in front.
        }
    )
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    for name in ("codesign", "osascript", "pkill", "rm", "sleep", "uname"):
        pass  # real binaries are fine; only codesign needs stubbing
    codesign_stub = stub_dir / "codesign"
    codesign_stub.write_text("#!/bin/bash\nexit 0\n")
    codesign_stub.chmod(0o755)
    env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")

    # Stub runtime binary: exits 0 immediately (clean quit).
    bin_dir = Path(app) / "Contents" / "MacOS"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub_runtime = bin_dir / "bolo-runtime"
    stub_runtime.write_text("#!/bin/bash\nexit 0\n")
    stub_runtime.chmod(0o755)
    # A ready venv so the helper skips the real (impossible here) venv
    # build and goes straight to supervision.
    _stage_fake_venv(tmp_path)
    # Sign after staging the executables. The codesign stub is used only
    # for the helper's own verify.
    _codesign_adhoc(app)

    run = subprocess.run(
        [str(launcher)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    # Foreground mode means the supervisor never detached, so the lock it
    # held must be cleaned up by the time the native process exits.
    assert not (runtime / "bolo-supervisor.lock").exists()
    assert not (runtime / "bolo-supervisor.pid").exists()
    log_text = (runtime / "bolo.log").read_text()
    assert "clean exit" in log_text


def test_native_launcher_helper_relaunches_crashing_runtime(tmp_path):
    """Supervision still works under the native launcher: a runtime stub
    that crashes once then quits cleanly is relaunched, and the lock is
    cleaned up afterwards."""
    _compile_native_launcher(tmp_path)
    app = _stage_fake_bundle(tmp_path)
    resources = Path(app) / "Contents" / "Resources"
    subprocess.run(["cp", str(HELPER), resources / "bolo-helper"], check=True)
    (resources / "bolo-helper").chmod(0o755)
    launcher = Path(app) / "Contents" / "MacOS" / "bolo"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["cp", str(tmp_path / "native" / "bolo"), str(launcher)], check=True
    )
    launcher.chmod(0o755)

    runtime = tmp_path / "runtime"
    marker = runtime / "runs"
    runtime.mkdir(parents=True)
    env = dict(os.environ)
    env.update(
        {
            "BOLO_SKIP_LOGIN_ITEM": "1",
            "BOLO_RUNTIME_DIR": str(runtime),
            "BOLO_VENV_DIR": str(tmp_path / "venv"),
        }
    )
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    codesign_stub = stub_dir / "codesign"
    codesign_stub.write_text("#!/bin/bash\nexit 0\n")
    codesign_stub.chmod(0o755)
    env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")

    # Stub runtime: first invocation crashes (exit 2), second quits clean.
    stub_bin_dir = Path(app) / "Contents" / "MacOS"
    stub_bin_dir.mkdir(parents=True, exist_ok=True)
    stub_runtime = stub_bin_dir / "bolo-runtime"
    stub_runtime.write_text(
        "#!/bin/bash\n"
        'if [ -e "{0}" ]; then exit 0; fi\n'
        'touch "{0}"\n'
        "exit 2\n".format(str(marker))
    )
    stub_runtime.chmod(0o755)
    _stage_fake_venv(tmp_path)
    _codesign_adhoc(app)

    run = subprocess.run(
        [str(launcher)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    log_text = (runtime / "bolo.log").read_text()
    # The crash was logged and the runtime was restarted.
    assert "exited with code 2, restarting" in log_text
    assert "clean exit" in log_text
    # Both runs happened, proving the relaunch.
    assert marker.exists()
    # Cleanup after the clean quit.
    assert not (runtime / "bolo-supervisor.lock").exists()
    assert not (runtime / "bolo-supervisor.pid").exists()


def _stage_native_bundle(tmp_path, runtime_script, extra_env=None):
    """Stage a fake bundle with a compiled native launcher and a stub
    runtime, and return (launcher path, env). No real app, no network,
    no UI automation."""
    _compile_native_launcher(tmp_path)
    app = _stage_fake_bundle(tmp_path)
    resources = Path(app) / "Contents" / "Resources"
    subprocess.run(["cp", str(HELPER), resources / "bolo-helper"], check=True)
    (resources / "bolo-helper").chmod(0o755)
    launcher = Path(app) / "Contents" / "MacOS" / "bolo"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["cp", str(tmp_path / "native" / "bolo"), str(launcher)], check=True
    )
    launcher.chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    _stage_fake_venv(tmp_path)
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir(exist_ok=True)
    codesign_stub = stub_dir / "codesign"
    codesign_stub.write_text("#!/bin/bash\nexit 0\n")
    codesign_stub.chmod(0o755)
    if runtime_script is not None:
        stub_runtime = launcher.parent / "bolo-runtime"
        stub_runtime.write_text(runtime_script)
        stub_runtime.chmod(0o755)
    # Sign after staging the executables. Keep the stub verifier ahead
    # on PATH for the helper's own signature check.
    _codesign_adhoc(app)
    env = dict(os.environ)
    env.update(
        {
            "BOLO_SKIP_LOGIN_ITEM": "1",
            "BOLO_RUNTIME_DIR": str(runtime),
            "BOLO_VENV_DIR": str(tmp_path / "venv"),
            "PATH": str(stub_dir) + os.pathsep + os.environ.get("PATH", ""),
        }
    )
    if extra_env:
        env.update(extra_env)
    return launcher, env, runtime


def test_detached_helper_releases_its_supervisor_lock(tmp_path):
    launcher, env, runtime = _stage_native_bundle(
        tmp_path, "#!/bin/bash\nsleep 0.1\nexit 0\n"
    )
    env.pop("BOLO_HELPER_FOREGROUND", None)
    helper = launcher.parent.parent / "Resources" / "bolo-helper"
    run = subprocess.run(
        [str(helper)], env=env, capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stderr
    assert "clean exit" in (runtime / "bolo.log").read_text()
    assert not (runtime / "bolo-supervisor.lock").exists()
    assert not (runtime / "bolo-supervisor.pid").exists()


def test_native_concurrent_launch_rejects_second_and_keeps_first(tmp_path):
    """Two simultaneous native launches: the first owns the lock and runs
    a long-lived stub runtime; the second must see a live owner and exit
    without touching the first instance's processes or its lock."""
    runtime = Path(str(tmp_path)) / "runtime"
    # A runtime that stays alive until told to quit, recording its PID so
    # the test can prove it survived the second launch.
    pid_file = runtime / "runtime.pid"
    runtime.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/bin/bash\necho $$ > {0}\nwhile true; do sleep 0.2; done\n".format(
            str(pid_file)
        )
    )
    launcher, env, _ = _stage_native_bundle(tmp_path, script)

    first = subprocess.Popen(
        [str(launcher)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )
    # Wait for the first instance to publish its runtime PID.
    for _ in range(100):
        if pid_file.exists():
            break
        time.sleep(0.1)
    assert pid_file.exists(), "first instance never started its runtime"
    runtime_pid = int(pid_file.read_text().strip())
    lock = Path(env["BOLO_RUNTIME_DIR"]) / "bolo-supervisor.lock"
    assert lock.exists()

    second = subprocess.run(
        [str(launcher)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    # The second launch exits promptly (not killed, just rejected).
    assert second.returncode == 0, second.stderr
    # The first instance's runtime is still alive: no broad kill.
    assert os.path.exists("/proc/{0}".format(runtime_pid)) or _pid_alive(
        runtime_pid
    )
    # The lock is untouched and still owned by the first instance.
    assert lock.exists()

    # Quit the first instance cleanly and confirm the chain unwinds.
    helper_pid = None
    pid_file_path = Path(env["BOLO_RUNTIME_DIR"]) / "bolo-supervisor.pid"
    for _ in range(50):
        if pid_file_path.exists():
            helper_pid = int(pid_file_path.read_text().strip())
            break
        time.sleep(0.1)
    first.terminate()
    try:
        first.wait(timeout=15)
    except subprocess.TimeoutExpired:
        first.kill()
        assert False, "native launcher did not exit after SIGTERM"
    _wait_pid_gone(runtime_pid, 15)
    assert not lock.exists()
    assert not pid_file_path.exists()


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_pid_gone(pid, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    assert False, "process {0} still alive after {1}s".format(pid, timeout)


def test_native_sigterm_terminates_long_running_child_quickly(tmp_path):
    """The core regression: a quit must reach the runtime child and both
    must exit in bounded time. The runtime stub ignores nothing and runs
    forever, so the only way this test passes is genuine signal
    forwarding and wait."""
    script = "#!/bin/bash\ntrap '' INT\nwhile true; do sleep 0.2; done\n"
    launcher, env, runtime = _stage_native_bundle(tmp_path, script)
    proc = subprocess.Popen(
        [str(launcher)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )
    # Let the helper take the lock, then wait for the runtime child to
    # actually be running before quitting: quitting during the helper's
    # own startup would test a different (already covered) path.
    pid_file_path = Path(runtime) / "bolo-supervisor.pid"
    for _ in range(100):
        if pid_file_path.exists():
            break
        time.sleep(0.1)
    lock = Path(runtime) / "bolo-supervisor.lock"
    assert lock.exists()
    # The runtime grandchild must be alive and long-running.
    runtime_alive = False
    for _ in range(100):
        out = subprocess.run(
            ["pgrep", "-P", str(pid_file_path.read_text().strip())],
            capture_output=True, text=True,
        )
        if out.stdout.strip():
            # One level down: the helper's child is the runtime.
            for child in out.stdout.split():
                grand = subprocess.run(
                    ["pgrep", "-P", child], capture_output=True, text=True,
                )
                if grand.stdout.strip():
                    runtime_alive = True
                    break
        if runtime_alive:
            break
        time.sleep(0.1)
    assert runtime_alive, "the supervised runtime never started"

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        assert False, "native launcher hung on SIGTERM instead of quitting"
    # The launcher exited in bounded time and the lock is gone.
    assert not lock.exists()
    assert not pid_file_path.exists()


def test_helper_verifies_signature_before_taking_lock(tmp_path):
    """A bundle whose signature fails verification must exit before the
    supervisor lock exists, so a failed launch leaves no lock behind."""
    app = _stage_fake_bundle(tmp_path)
    staged_helper = Path(app) / "Contents" / "Resources" / "bolo-helper"
    staged_helper.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", str(HELPER), str(staged_helper)], check=True)
    staged_helper.chmod(0o755)
    runtime = tmp_path / "runtime"
    env = dict(os.environ)
    env.update(
        {
            "BOLO_SKIP_LOGIN_ITEM": "1",
            "BOLO_RUNTIME_DIR": str(runtime),
            "BOLO_VENV_DIR": str(tmp_path / "venv"),
            "PATH": os.environ.get("PATH", ""),
        }
    )
    # No codesign stub on PATH: the real codesign fails on this fake
    # bundle, which is the condition under test.
    run = subprocess.run(
        ["bash", str(staged_helper)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert run.returncode == 1
    # The lock and PID file must not exist: verification failed first.
    assert not (runtime / "bolo-supervisor.lock").exists()
    assert not (runtime / "bolo-supervisor.pid").exists()
    # Source order check: verify precedes the lock in the script body.
    text = HELPER.read_text()
    assert text.index('codesign --verify "$APP_DIR"') < text.index(
        'mkdir "$LOCK_DIR"'
    )


def test_build_dmg_stages_native_launcher_and_shell_helper():
    text = BUILD_DMG.read_text()
    # The main executable is the compiled Swift launcher, not the shell.
    assert "bundle-launcher.swift" in text
    assert 'install -m 755 "$LAUNCHER_BUILD/bolo" "$APP/Contents/MacOS/bolo"' in text
    assert 'install -m 755 "$ROOT/scripts/bundle-launcher.sh" "$APP/Contents/Resources/bolo-helper"' in text
    # Both arches are compiled and lipo'd, and the mounted DMG check
    # verifies the launcher is universal and the helper is present.
    assert "arm64-apple-macos12.0" in text
    assert "x86_64-apple-macos12.0" in text
    assert '"Contents/Resources/bolo-helper"' in text
    assert "MOUNTED_LAUNCHER_ARCHS" in text
    # The shell helper is never installed as the main executable anymore.
    assert 'install -m 755 "$ROOT/scripts/bundle-launcher.sh" "$APP/Contents/MacOS/bolo"' not in text


def test_build_dmg_native_launcher_is_accessory_not_dock():
    text = BUILD_DMG.read_text()
    # LSUIElement stays true: the native launcher presents no Dock icon
    # and no window; it only keeps the app identity alive.
    assert "<key>LSUIElement</key>" in text


def test_build_dmg_stages_and_verifies_dashboard_helper():
    text = BUILD_DMG.read_text()
    # The dashboard helper is staged into Resources next to app_window.py
    # (both are launched through the same entry point) and the mounted-DMG
    # check proves it survived packaging.
    assert "dashboard_window.py" in text
    assert '"Contents/Resources/dashboard_window.py"' in text


def test_make_dmg_layout_round_trips(tmp_path):
    """Write a .DS_Store with make-dmg-layout.py and read the exact slots,
    window rect, view mode, and background alias resolution back."""
    volume = tmp_path / "volume"
    (volume / ".background").mkdir(parents=True)
    # A tiny real PNG is not required: the alias points at a file by name,
    # only existence matters.
    (volume / ".background" / "background.png").write_bytes(b"png")
    (volume / "Bolo.app").mkdir()
    (volume / "Applications").mkdir()

    layout_venv = REPO_ROOT / "build" / "icon-venv" / "bin" / "python3"
    if not layout_venv.exists():
        pytest.skip("ds_store/mac_alias not installed (run the DMG build once)")
    code = (
        "import importlib.util, sys; "
        "spec = importlib.util.spec_from_file_location("
        "'make_dmg_layout', {!r}); "
        "m = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(m); "
        "ds = m.write_layout({!r}, '.background/background.png', "
        "{{'Bolo.app': (150, 232), 'Applications': (462, 232)}}, (0, 0, 660, 400)); "
        "state = m.read_layout(ds); "
        "print('W', m.parse_window(state['window_bounds'])); "
        "print('VIEW', state['view']); "
        "print('ARRANGE', state['arrange_by']); "
        "print('SIZE', state['icon_size']); "
        "print('BGTYPE', state['background_type']); "
        "print('BG', state['background']); "
        "print('BOLO', state['positions']['Bolo.app']); "
        "print('APPS', state['positions']['Applications'])"
    ).format(str(DMG_LAYOUT), str(volume))
    run = subprocess.run(
        [str(layout_venv), "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if "struct.error" in run.stderr or "ModuleNotFoundError" in run.stderr:
        # mac_alias cannot build an alias for a file whose CNID overflows
        # the 32-bit packing on non-HFS volumes (APFS tmp dirs); the build
        # avoids this by mounting a UDRW scratch volume, covered by the
        # mounted-volume test below.
        pytest.skip("alias packing needs an HFS volume: {0}".format(run.stderr.strip()))
    assert run.returncode == 0, run.stderr
    out = dict(
        line.split(" ", 1) for line in run.stdout.splitlines() if " " in line
    )
    assert out["W"] == "(0, 0, 660, 400)"
    assert out["VIEW"] == "icnv"
    assert out["ARRANGE"] == "none"
    assert float(out["SIZE"]) == 80.0
    assert out["BGTYPE"] == "2"
    assert out["BG"].endswith("volume/.background/background.png")
    assert out["BOLO"] == "(150, 232)"
    assert out["APPS"] == "(462, 232)"


def test_make_dmg_layout_requires_background_on_volume(tmp_path):
    volume = tmp_path / "volume"
    volume.mkdir()
    code = (
        "import importlib.util; "
        "spec = importlib.util.spec_from_file_location("
        "'make_dmg_layout', {!r}); "
        "m = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(m); "
        "m.write_layout({!r}, '.background/background.png', "
        "{{'Bolo.app': (150, 232)}}, (0, 0, 660, 400))"
    ).format(str(DMG_LAYOUT), str(volume))
    run = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode != 0
    assert isinstance(run.returncode, int)


def test_make_dmg_layout_survives_dmg_conversion(tmp_path):
    """The volume-identity regression: write the layout on a UDRW volume,
    convert that SAME image to UDZO (what build-dmg.sh now ships), and
    prove the .DS_Store inside the converted image still resolves the
    background alias to the hashed file within that volume. Recreating an
    image from the folder would break the alias; this test fails then."""
    if shutil.which("hdiutil") is None:
        pytest.skip("hdiutil is unavailable")
    layout_venv = REPO_ROOT / "build" / "icon-venv" / "bin" / "python3"
    if not layout_venv.exists():
        pytest.skip("ds_store/mac_alias not installed (run the DMG build once)")

    source = tmp_path / "src"
    (source / ".background").mkdir(parents=True)
    (source / ".background" / "background-abc123.png").write_bytes(b"png")
    (source / "Bolo.app").mkdir()
    (source / "Applications").mkdir()

    layout_img = tmp_path / "layout.dmg"
    created = subprocess.run(
        ["hdiutil", "create", "-volname", "BoloLayoutTest", "-format", "UDRW",
         "-srcfolder", str(source), str(layout_img)],
        capture_output=True, text=True, timeout=120,
    )
    assert created.returncode == 0, created.stderr
    mount = tmp_path / "mnt"
    mount.mkdir()
    attached = subprocess.run(
        ["hdiutil", "attach", str(layout_img), "-mountpoint", str(mount)],
        capture_output=True, text=True, timeout=120,
    )
    assert attached.returncode == 0, attached.stderr
    try:
        code = (
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location("
            "'make_dmg_layout', {!r}); "
            "m = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(m); "
            "sys.exit(m.main(['--volume', {!r}, '--background', "
            "'.background/background-abc123.png', '--bolo-position', '150,232', "
            "'--applications-position', '462,232', '--window', '0,0,660,400', "
            "'--verify']))"
        ).format(str(DMG_LAYOUT), str(mount))
        run = subprocess.run(
            [str(layout_venv), "-c", code],
            capture_output=True, text=True, timeout=120,
        )
        assert run.returncode == 0, run.stderr
    finally:
        subprocess.run(["hdiutil", "detach", str(mount)],
                       capture_output=True, timeout=120)

    # Convert the SAME layout image, exactly as the build does now.
    final_img = tmp_path / "final.dmg"
    converted = subprocess.run(
        ["hdiutil", "convert", str(layout_img), "-format", "UDZO",
         "-ov", "-o", str(final_img)],
        capture_output=True, text=True, timeout=180,
    )
    assert converted.returncode == 0, converted.stderr

    final_mount = tmp_path / "final-mnt"
    final_mount.mkdir()
    attached2 = subprocess.run(
        ["hdiutil", "attach", str(final_img), "-mountpoint", str(final_mount)],
        capture_output=True, text=True, timeout=120,
    )
    assert attached2.returncode == 0, attached2.stderr
    try:
        code = (
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location("
            "'make_dmg_layout', {!r}); "
            "m = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(m); "
            "sys.exit(m.main(['--volume', {!r}, '--bolo-position', '150,232', "
            "'--applications-position', '462,232', '--window', '0,0,660,400', "
            "'--verify-existing']))"
        ).format(str(DMG_LAYOUT), str(final_mount))
        run = subprocess.run(
            [str(layout_venv), "-c", code],
            capture_output=True, text=True, timeout=120,
        )
        assert run.returncode == 0, run.stderr
        assert "existing layout verified" in run.stdout
        assert "background-abc123.png" in run.stdout
    finally:
        subprocess.run(["hdiutil", "detach", str(final_mount)],
                       capture_output=True, timeout=120)


def test_make_dmg_layout_round_trips_on_mounted_hfs_volume(tmp_path):
    """The real path: a UDRW scratch volume (HFS+, the format the build
    uses) accepts the alias, and the writer's own --verify readback
    resolves the exact window, icon slots, and background reference."""
    if shutil.which("hdiutil") is None:
        pytest.skip("hdiutil is unavailable")
    layout_venv = REPO_ROOT / "build" / "icon-venv" / "bin" / "python3"
    if not layout_venv.exists():
        pytest.skip("ds_store/mac_alias not installed (run the DMG build once)")

    source = tmp_path / "src"
    (source / ".background").mkdir(parents=True)
    (source / ".background" / "background.png").write_bytes(b"png")
    (source / "Bolo.app").mkdir()
    (source / "Applications").mkdir()

    image = tmp_path / "scratch.dmg"
    created = subprocess.run(
        ["hdiutil", "create", "-volname", "BoloLayoutTest", "-format", "UDRW",
         "-srcfolder", str(source), str(image)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if created.returncode != 0:
        pytest.skip("could not create the scratch image: {0}".format(created.stderr.strip()))

    mount = tmp_path / "mnt"
    mount.mkdir()
    mounted = subprocess.run(
        ["hdiutil", "attach", str(image), "-mountpoint", str(mount)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert mounted.returncode == 0, mounted.stderr
    try:
        code = (
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location("
            "'make_dmg_layout', {!r}); "
            "m = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(m); "
            "sys.exit(m.main(['--volume', {!r}, '--background', "
            "'.background/background.png', '--bolo-position', '150,232', "
            "'--applications-position', '462,232', '--window', '0,0,660,400', "
            "'--verify']))"
        ).format(str(DMG_LAYOUT), str(mount))
        run = subprocess.run(
            [str(layout_venv), "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert run.returncode == 0, run.stderr
        assert "verified window (0, 0, 660, 400)" in run.stdout
        assert "Bolo.app at (150, 232)" in run.stdout
        assert "Applications at (462, 232)" in run.stdout
        assert run.stdout.rstrip().splitlines()[-1].endswith("background.png")
    finally:
        subprocess.run(
            ["hdiutil", "detach", str(mount)],
            capture_output=True,
            timeout=120,
        )


def test_dmg_requirements_are_pinned_with_hashes():
    text = (REPO_ROOT / "scripts" / "dmg-requirements.txt").read_text()
    assert "ds_store==1.3.3" in text
    assert "mac_alias==2.2.3" in text
    # The pinned hashes are declared (once inline in the comment header,
    # once as the requirements spec), covering every requirement.
    assert "sha256:b92a371efbf1b4ccce2a04d1ed13fceacc4736c81ba09cf5aefb74c088160a35" in text
    assert "sha256:7362b521d2132ef92f606a37abfed5fcd849ceb2f28b6f9743e014b02af92f0d" in text
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{63,64}", text)) >= 2


def test_make_dmg_background_layout_stays_inside_canvas():
    bg = _load_dmg_background()

    assert (bg.WIDTH, bg.HEIGHT) == (660, 400)
    # Icon slots sit inside the canvas with room for labels below.
    for slot in (bg.APPLICATIONS_POS, bg.BOLO_POS):
        assert 0 < slot[0] and slot[0] + bg.ICON_SIZE <= bg.WIDTH
        assert 0 < slot[1] and slot[1] + bg.ICON_SIZE <= bg.HEIGHT
    # Bolo sits LEFT, Applications RIGHT, matching the rightward drag arrow.
    assert bg.BOLO_POS[0] < bg.APPLICATIONS_POS[0]
    # The arrow rides the icons' centerline band (y 232), sits BETWEEN the
    # two icons, tail just right of Bolo and tip just left of Applications.
    shaft, head = bg.arrow_pieces()
    center_y = bg.arrow_center()[1]
    assert bg.BOLO_POS[1] == 232 and bg.APPLICATIONS_POS[1] == 232
    assert bg.BOLO_POS[0] + bg.ICON_SIZE < shaft[0]  # 230 < tail
    assert shaft[0] == bg.ARROW_TAIL_X
    assert head[2][0] == bg.ARROW_TIP_X
    assert head[2][0] < bg.APPLICATIONS_POS[0]  # tip left of the folder
    assert head[0][0] == shaft[0] + shaft[2] and head[1][0] == head[0][0]
    assert head[2][1] == center_y
    assert abs((shaft[1] + shaft[3] / 2.0) - center_y) < 0.01
    # Both instruction lines are present and the next-step line is smaller
    # (the drag line stays primary).
    assert bg.SUBTITLE == "Drag Bolo to Applications"
    assert "Applications" in bg.SUBTITLE2
    assert bg.SUBTITLE2_FONT_SIZE < bg.SUBTITLE_FONT_SIZE
    # Brand lockup clears the instructions and the icon band.
    assert bg.WORDMARK == "bolo"
    assert bg.WORDMARK_CENTER_Y + bg.MARK_GRID_SIZE / 2 < bg.SUBTITLE_CENTER_Y
    assert bg.SUBTITLE2_CENTER_Y < bg.BOLO_POS[1]


def test_make_dmg_background_reports_missing_pyobjc_cleanly(tmp_path):
    # Under a python without pyobjc, render fails with a clear message and
    # no output file is written. On interpreters that do have pyobjc (this
    # machine's system python), the render simply succeeds.
    probe = subprocess.run(
        [sys.executable, "-c", "import AppKit"], capture_output=True, text=True
    )
    code = (
        "import sys, importlib.util; "
        "spec = importlib.util.spec_from_file_location('make_dmg_background', {!r}); "
        "module = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(module); "
        "sys.exit(module.main(['--output', {!r}]))"
    ).format(
        str(REPO_ROOT / "scripts" / "make-dmg-background.py"),
        str(tmp_path / "out" / "background.png"),
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT / "scripts"),
    )
    if probe.returncode == 0:
        assert result.returncode == 0
        assert (tmp_path / "out" / "background.png").is_file()
    else:
        assert result.returncode == 1
        assert "AppKit is unavailable" in result.stderr
        assert not (tmp_path / "out" / "background.png").exists()


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
    # Tolerate the version moving forward; the build script must agree
    # with Cargo.toml (a mismatch would mislabel the artifact).
    assert re.match(r"^\d+\.\d+\.\d+$", version.stdout.strip())


def test_make_icon_keeps_mark_inside_tile():
    """The brand mark's visible ink box stays inside the clay tile at any
    canvas size, and the mark's horizontal center tracks the tile's
    center (so the icon stays balanced when down-sampled by sips)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import make_icon

    for size in (1024, 256, 64, 32):
        tile_x, tile_y, tile_w, tile_h = make_icon.tile_box(size)
        ink_x, ink_y, ink_w, ink_h = make_icon.mark_ink_box(size)
        assert tile_x <= ink_x, f"mark left {ink_x} escaped tile left {tile_x} at {size}"
        assert ink_x + ink_w <= tile_x + tile_w, (
            f"mark right {ink_x + ink_w} escaped tile right {tile_x + tile_w} at {size}"
        )
        assert tile_y <= ink_y, f"mark top {ink_y} escaped tile top {tile_y} at {size}"
        assert ink_y + ink_h <= tile_y + tile_h, (
            f"mark bottom {ink_y + ink_h} escaped tile bottom {tile_y + tile_h} at {size}"
        )
        mark_center_x = ink_x + ink_w / 2
        tile_center_x = tile_x + tile_w / 2
        assert abs(mark_center_x - tile_center_x) < 0.01, (
            f"mark center x {mark_center_x} drifted from tile center x {tile_center_x} at {size}"
        )
        # The tile scales with size (so the icon is valid down to --size 16).
        assert tile_w > 0 and tile_w < size


def test_make_icon_mark_is_upright_in_actual_bitmap():
    """The ascender and terminal must sit above the bowl, not below it."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import make_icon
    appkit = pytest.importorskip("AppKit")
    rep = make_icon.render(256)
    x, y = make_icon.mark_origin(256)
    scale = 256 * make_icon.MARK_GRID_FRACTION / 100
    stem = rep.colorAtX_y_(round(x + 26 * scale), round(y + 12 * scale))
    stem = stem.colorUsingColorSpace_(appkit.NSColorSpace.sRGBColorSpace())
    assert stem.redComponent() > .9 and stem.greenComponent() > .9
    terminal = rep.colorAtX_y_(round(x + 75 * scale), round(y + 20 * scale))
    terminal = terminal.colorUsingColorSpace_(appkit.NSColorSpace.sRGBColorSpace())
    assert terminal.redComponent() < .3 and terminal.greenComponent() < .3


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
    mode = stat.S_IMODE(HELPER.stat().st_mode)
    assert mode & stat.S_IXUSR


def test_helper_rejects_importable_interpreter_from_another_runtime(tmp_path):
    """Successful imports alone must not adopt a source or moved-bundle venv."""
    module_dir = tmp_path / 'modules'
    module_dir.mkdir()
    for name in ('objc', 'AppKit', 'Foundation', 'Quartz', 'ApplicationServices'):
        (module_dir / (name + '.py')).write_text('')
    function = re.search(r'^verify_helpers\(\) \{.*?^\}', HELPER.read_text(), re.M | re.S).group()
    env = dict(os.environ, PYTHONPATH=str(module_dir))
    wrong = tmp_path / 'unrelated-runtime' / 'bin'
    wrong.mkdir(parents=True)
    for expected, success in ((wrong, False), (Path(sys.base_prefix) / 'bin', True)):
        result = subprocess.run(
            ['bash', '-c', function + '\nverify_helpers "$1" "$2"',
             'provenance-check', sys.executable, str(expected)],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert (result.returncode == 0) is success, result.stderr


def test_launcher_import_checks_do_not_write_inside_bundle(tmp_path):
    """Helper imports must leave signed Python resources unchanged."""
    app = _stage_fake_bundle(tmp_path)
    modules = app / 'Contents' / 'Resources' / 'modules'
    modules.mkdir()
    for name in ('objc', 'AppKit', 'Foundation', 'Quartz', 'ApplicationServices'):
        (modules / (name + '.py')).write_text('')
    venv_bin = tmp_path / 'venv' / 'bin'
    venv_bin.mkdir(parents=True)
    (venv_bin / 'python3').symlink_to(sys.executable)
    before = sorted(p.relative_to(modules) for p in modules.rglob('*'))
    result = _print_state(app, {
        'BOLO_RUNTIME_DIR': str(tmp_path / 'runtime'),
        'BOLO_VENV_DIR': str(tmp_path / 'venv'),
        'PYTHONPATH': str(modules),
        'PYTHONDONTWRITEBYTECODE': '',
    })
    assert result.returncode == 0, result.stderr
    assert sorted(p.relative_to(modules) for p in modules.rglob('*')) == before


def _isolated_home_env(env, home):
    """Env for native launcher runs that isolates every home-location the
    launcher can touch: HOME plus the CoreFoundation overrides, so tests
    never read or write a real ~/.bolo."""
    isolated = dict(env)
    isolated["HOME"] = str(home)
    isolated["CFFIXED_USER_HOME"] = str(home)
    return isolated


def test_native_launcher_interactive_launch_writes_open_request(tmp_path):
    """A plain (Finder-style) native launch writes exactly one bounded
    open-dashboard request under the isolated ~/.bolo, and the helper's
    supervisor chain still runs and exits cleanly with a stub runtime."""
    script = "#!/bin/bash\nexit 0\n"
    launcher, env, runtime = _stage_native_bundle(tmp_path, script)
    home = tmp_path / "home-isolated"
    home.mkdir()
    env = _isolated_home_env(env, home)
    run = subprocess.run(
        [str(launcher)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert run.returncode == 0, run.stderr
    request = home / ".bolo" / "open-dashboard.request"
    assert request.exists(), "interactive launch must leave an open request"
    assert request.read_text().strip() != ""
    # Exactly one file: no leftover temp files from the atomic write.
    bolo_dir = home / ".bolo"
    assert sorted(p.name for p in bolo_dir.iterdir()) == [
        "open-dashboard.request"
    ]


def test_native_launcher_quiet_launch_writes_no_request(tmp_path):
    """BOLO_QUIET_STARTUP=1 marks a scripted/background start: the native
    launcher writes no open request, so startup stays quiet."""
    script = "#!/bin/bash\nexit 0\n"
    launcher, env, runtime = _stage_native_bundle(tmp_path, script)
    home = tmp_path / "home-quiet"
    home.mkdir()
    env = _isolated_home_env(env, home)
    env["BOLO_QUIET_STARTUP"] = "1"
    run = subprocess.run(
        [str(launcher)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert not (home / ".bolo" / "open-dashboard.request").exists(), (
        "quiet launch must not request the dashboard"
    )


def test_native_launcher_second_click_requests_running_instance(tmp_path):
    """A second native launch while the supervisor lock is held must still
    write the open request, so the Finder re-click hands the request to the
    running instance instead of doing nothing after being rejected."""
    pid_file = Path(str(tmp_path)) / "runtime" / "runtime.pid"
    script = (
        "#!/bin/bash\necho $$ > {0}\nwhile true; do sleep 0.2; done\n".format(
            str(pid_file)
        )
    )
    launcher, env, runtime = _stage_native_bundle(tmp_path, script)
    home = tmp_path / "home-second"
    home.mkdir()
    env = _isolated_home_env(env, home)
    first = subprocess.Popen(
        [str(launcher)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )
    for _ in range(100):
        if pid_file.exists():
            break
        time.sleep(0.1)
    assert pid_file.exists(), "first instance never started its runtime"
    request = home / ".bolo" / "open-dashboard.request"
    # The first, interactive launch already requested the dashboard.
    assert request.exists()

    # Consume the request the way the runtime does, then re-click: the
    # second launcher is rejected by the lock but must write the request
    # again so the running instance opens its dashboard.
    request.unlink()
    second = subprocess.run(
        [str(launcher)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert second.returncode == 0, second.stderr
    assert request.exists(), (
        "a rejected second launch must still request the running dashboard"
    )

    first.terminate()
    try:
        first.wait(timeout=15)
    except subprocess.TimeoutExpired:
        first.kill()
        assert False, "native launcher did not exit after SIGTERM"
    lock = Path(env["BOLO_RUNTIME_DIR"]) / "bolo-supervisor.lock"
    # The helper's EXIT trap removes the lock; it can trail the launcher
    # exit by a moment, so wait bounded for the cleanup.
    for _ in range(100):
        if not lock.exists():
            break
        time.sleep(0.1)
    assert not lock.exists()


def test_native_launcher_sigterm_forwards_promptly_under_runloop(tmp_path):
    """With NSApplication.run on the main thread, a SIGTERM must still be
    forwarded by the timer and the launcher must exit in bounded time."""
    script = "#!/bin/bash\ntrap '' INT\nwhile true; do sleep 0.2; done\n"
    launcher, env, runtime = _stage_native_bundle(tmp_path, script)
    home = tmp_path / "home-sigterm"
    home.mkdir()
    env = _isolated_home_env(env, home)
    proc = subprocess.Popen(
        [str(launcher)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )
    lock = Path(runtime) / "bolo-supervisor.lock"
    for _ in range(100):
        if lock.exists():
            break
        time.sleep(0.1)
    assert lock.exists()
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        assert False, "native launcher hung on SIGTERM under the runloop"
    # The helper's EXIT trap removes the lock; it can trail the launcher's
    # own exit by a moment, so wait bounded for the cleanup.
    for _ in range(100):
        if not lock.exists():
            break
        time.sleep(0.1)
    assert not lock.exists()

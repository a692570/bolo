#!/bin/bash
# Build a distributable Bolo-<version>.dmg in one shot.
#
# Phases (each logs a [dmg] marker and fails loudly):
#   1. universal Rust binary (aarch64 via the system cargo; x86_64 via a
#      rustup toolchain bootstrapped inside build/ when the system rust has
#      no cross-target std, as with Homebrew rust)
#   2. bundled python-build-standalone runtimes, pinned by URL + SHA256
#   3. pyobjc wheels (resolved from helper-requirements.txt, cached)
#   4. app icon (scripts/make_icon.py under a build-time venv)
#   5. Bolo.app staging (launcher, binary, helpers, runtimes, wheels, plist)
#   6. ad-hoc codesign (no real identity, never notarized)
#   6.5 Finder presentation: background image + icon positions via a
#       read-write scratch volume whose .DS_Store lands in the staging root
#   7. DMG creation with an /Applications symlink and the Finder layout
#   8. verification: mount, structure, universal binary, codesign, size
#
# Artifacts land in build/ (cached across runs) and dist/Bolo-<version>.dmg.
# The user-facing install flow is: open the DMG, drag Bolo to Applications,
# launch; the bundle creates ~/.bolo/venv offline from the bundled wheels and
# collects the AssemblyAI key in the onboarding window.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
DIST="$ROOT/dist"
DOWNLOADS="$BUILD/downloads"
PYBUILD="$BUILD/python"
WHEELS="$BUILD/wheels"
ICON_DIR="$BUILD/icon"
RUSTUP_HOME="$BUILD/rustup"
CARGO_HOME_X86="$BUILD/cargo-home"

# python-build-standalone CPython 3.12.15, release 20261001 (pinned).
PBS_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/20261001"
PBS_AARCH64_URL="$PBS_BASE/cpython-3.12.15%2B20261001-aarch64-apple-darwin-install_only.tar.gz"
PBS_AARCH64_SHA="f1ee170bd7bb45bea526c4f9489b41f9c5d978c4cd7a08fd11809de56c39b736"
PBS_X86_64_URL="$PBS_BASE/cpython-3.12.15%2B20261001-x86_64-apple-darwin-install_only.tar.gz"
PBS_X86_64_SHA="f350524f5b80b9e1aeb25e39570e59f75edb6926582dc8f6edf2d0c744a7997c"

log() { echo "[dmg] $*"; }
fail() { echo "[dmg] ERROR: $*" >&2; exit 1; }

cd "$ROOT"

# --- Phase 0: prerequisites and version ---------------------------------------

for tool in lipo hdiutil iconutil sips codesign cargo curl; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required but was not found"
done

VERSION="$(sed -n 's/^version *= *"\([^"]*\)"/\1/p' Cargo.toml | head -1)"
[ -n "$VERSION" ] || fail "could not read the version from Cargo.toml"
log "building Bolo $VERSION"

mkdir -p "$BUILD" "$DIST" "$DOWNLOADS" "$WHEELS" "$ICON_DIR" "$PYBUILD"

# --- Phase 1: universal Rust binary ---------------------------------------------

system_sysroot="$(rustc --print sysroot)"
have_aarch64_std=false
have_x86_64_std=false
if [ -d "$system_sysroot/lib/rustlib/aarch64-apple-darwin" ]; then have_aarch64_std=true; fi
if [ -d "$system_sysroot/lib/rustlib/x86_64-apple-darwin" ]; then have_x86_64_std=true; fi
if [ -d "$system_sysroot/lib/rustlib/apple-x86_64" ]; then have_x86_64_std=true; fi

# Bootstrap a self-contained rustup toolchain inside build/ when the system
# rust cannot cross-compile for x86_64 (Homebrew rust ships host std only).
X86_CARGO=""
if [ "$have_x86_64_std" = false ]; then
    if [ ! -x "$CARGO_HOME_X86/bin/cargo" ]; then
        case "$(uname -m)" in
            arm64) RUSTUP_DIST_ARCH="aarch64-apple-darwin" ;;
            *) RUSTUP_DIST_ARCH="x86_64-apple-darwin" ;;
        esac
        log "bootstrapping rustup inside build/ for the x86_64 target"
        curl -sSfL "https://static.rust-lang.org/rustup/dist/$RUSTUP_DIST_ARCH/rustup-init" \
            -o "$DOWNLOADS/rustup-init" || fail "could not download rustup-init"
        chmod +x "$DOWNLOADS/rustup-init"
        RUSTUP_HOME="$RUSTUP_HOME" CARGO_HOME="$CARGO_HOME_X86" "$DOWNLOADS/rustup-init" -y \
            --no-modify-path --profile minimal --default-toolchain stable \
            >/dev/null || fail "rustup bootstrap failed"
    fi
    RUSTUP_HOME="$RUSTUP_HOME" CARGO_HOME="$CARGO_HOME_X86" \
        "$CARGO_HOME_X86/bin/rustup" target add x86_64-apple-darwin \
        >/dev/null 2>&1 || fail "rustup could not add the x86_64-apple-darwin target"
    X86_CARGO="$CARGO_HOME_X86/bin/cargo"
fi

log "building the Rust runtime for aarch64-apple-darwin"
if [ "$have_aarch64_std" = true ]; then
    cargo build --release --target aarch64-apple-darwin || fail "aarch64 Rust build failed"
    AARCH64_BIN="$ROOT/target/aarch64-apple-darwin/release/bolo"
else
    [ -n "$X86_CARGO" ] || fail "system rust has no aarch64 std and no rustup fallback"
    RUSTUP_HOME="$RUSTUP_HOME" CARGO_HOME="$CARGO_HOME_X86" "$X86_CARGO" build \
        --release --target aarch64-apple-darwin || fail "aarch64 Rust build failed"
    AARCH64_BIN="$ROOT/target/aarch64-apple-darwin/release/bolo"
fi
[ -x "$AARCH64_BIN" ] || fail "aarch64 binary missing after build"

log "building the Rust runtime for x86_64-apple-darwin"
if [ -n "$X86_CARGO" ]; then
    RUSTUP_HOME="$RUSTUP_HOME" CARGO_HOME="$CARGO_HOME_X86" "$X86_CARGO" build \
        --release --target x86_64-apple-darwin || fail "x86_64 Rust build failed"
else
    cargo build --release --target x86_64-apple-darwin || fail "x86_64 Rust build failed"
fi
X86_64_BIN="$ROOT/target/x86_64-apple-darwin/release/bolo"
[ -x "$X86_64_BIN" ] || fail "x86_64 binary missing after build"

mkdir -p "$BUILD/universal"
lipo -create "$AARCH64_BIN" "$X86_64_BIN" -output "$BUILD/universal/bolo" \
    || fail "lipo could not merge the two binaries"
strip -x "$BUILD/universal/bolo" >/dev/null 2>&1 || log "strip skipped"
UNIVERSAL_ARCHS="$(lipo -archs "$BUILD/universal/bolo")"
[ "$UNIVERSAL_ARCHS" = "x86_64 arm64" ] || [ "$UNIVERSAL_ARCHS" = "arm64 x86_64" ] \
    || fail "universal binary is not two-arch (got: $UNIVERSAL_ARCHS)"
log "universal binary ready ($UNIVERSAL_ARCHS, $(du -h "$BUILD/universal/bolo" | cut -f1))"

# --- Phase 2: bundled python runtimes --------------------------------------------

download_pinned() {
    local url="$1" sha="$2" dest="$3"
    local name
    name="$(basename "$dest")"
    if [ -f "$dest" ] && [ "$(shasum -a 256 "$dest" | cut -d' ' -f1)" = "$sha" ]; then
        log "using cached $name"
        return
    fi
    rm -f "$dest"
    log "downloading $name"
    curl -sSfL "$url" -o "$dest" || fail "download failed: $url"
    [ "$(shasum -a 256 "$dest" | cut -d' ' -f1)" = "$sha" ] \
        || fail "SHA256 mismatch for $name (expected $sha)"
}

extract_pbs() {
    local arch="$1" url="$2" sha="$3"
    local tarball="$DOWNLOADS/cpython-3.12.15+20261001-$arch-apple-darwin-install_only.tar.gz"
    if [ ! -d "$PYBUILD/$arch/python" ]; then
        download_pinned "$url" "$sha" "$tarball"
        rm -rf "$PYBUILD/$arch"
        mkdir -p "$PYBUILD/$arch"
        tar -xzf "$tarball" -C "$PYBUILD/$arch" || fail "could not extract the $arch python runtime"
    else
        log "using cached $arch python runtime"
    fi
    # Size guard: strip bytecode caches and optional stdlib dirs the helpers
    # never touch. python-build-standalone is already lean, so this is mostly
    # future-proofing; report what was removed.
    local before after removed
    before="$(du -sk "$PYBUILD/$arch" | cut -f1)"
    find "$PYBUILD/$arch" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
    rm -rf "$PYBUILD/$arch/python/lib/python3.12/test" \
        "$PYBUILD/$arch/python/lib/python3.12/idlelib" \
        "$PYBUILD/$arch/python/lib/python3.12/lib2to3" \
        "$PYBUILD/$arch/python/lib/python3.12/tkinter" \
        "$PYBUILD/$arch/python/lib/python3.12/turtledemo" 2>/dev/null || true
    after="$(du -sk "$PYBUILD/$arch" | cut -f1)"
    removed=$((before - after))
    [ "$removed" -lt 0 ] && removed=0
    log "$arch python runtime: $(du -h "$PYBUILD/$arch" | cut -f1) (stripped ${removed}KB)"
}

extract_pbs aarch64 "$PBS_AARCH64_URL" "$PBS_AARCH64_SHA"
extract_pbs x86_64 "$PBS_X86_64_URL" "$PBS_X86_64_SHA"

# --- Phase 3: pyobjc wheels --------------------------------------------------------

BUNDLED_PY="$PYBUILD/aarch64/python/bin/python3.12"
[ -x "$BUNDLED_PY" ] || fail "bundled aarch64 python is missing"
if [ -n "$(find "$WHEELS" -name '*.whl' -print -quit 2>/dev/null)" ]; then
    log "using cached wheels in $WHEELS"
else
    log "resolving and downloading pyobjc wheels (universal2, cp312)"
    # pyobjc publishes universal2 wheels, so one download covers both arches;
    # both --platform values are still passed so pip would fetch split
    # wheels too if the project ever stops shipping universal2.
    "$BUNDLED_PY" -m pip download \
        --only-binary=:all: \
        --python-version 3.12 --implementation cp --abi cp312 \
        --platform macosx_11_0_arm64 --platform macosx_11_0_x86_64 \
        -d "$WHEELS" \
        pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz \
        pyobjc-framework-ApplicationServices pyobjc-framework-CoreText \
        || fail "pyobjc wheel download failed"
fi

# Build-time self-test: install from the wheels offline exactly the way the
# launcher's first run does, and import the helper stack. The same venv then
# renders the app icon, so the icon script needs no system pyobjc.
ICON_VENV="$BUILD/icon-venv"
rm -rf "$ICON_VENV"
"$BUNDLED_PY" -m venv "$ICON_VENV" >/dev/null || fail "could not create the icon venv"
"$ICON_VENV/bin/python3" -m pip install --no-index --no-input \
    --disable-pip-version-check \
    --find-links "$WHEELS" -r "$ROOT/helper-requirements.txt" >/dev/null \
    || fail "bundled wheels failed to satisfy helper-requirements.txt"
"$ICON_VENV/bin/python3" -c 'import objc, AppKit, Foundation, Quartz, ApplicationServices' \
    || fail "helper stack does not import from the bundled wheels"
log "wheels verified against helper-requirements.txt"

# --- Phase 4: app icon ---------------------------------------------------------------

if [ ! -f "$ICON_DIR/Bolo.icns" ]; then
    log "rendering the app icon"
    "$ICON_VENV/bin/python3" "$ROOT/scripts/make_icon.py" \
        --output "$ICON_DIR/bolo-icon-1024.png" || fail "icon render failed"
    rm -rf "$ICON_DIR/Bolo.iconset"
    mkdir -p "$ICON_DIR/Bolo.iconset"
    for size in 16 32 128 256 512; do
        sips -z "$size" "$size" "$ICON_DIR/bolo-icon-1024.png" \
            --out "$ICON_DIR/Bolo.iconset/icon_${size}x${size}.png" >/dev/null \
            || fail "icon resize failed"
        double=$((size * 2))
        sips -z "$double" "$double" "$ICON_DIR/bolo-icon-1024.png" \
            --out "$ICON_DIR/Bolo.iconset/icon_${size}x${size}@2x.png" >/dev/null \
            || fail "icon resize failed"
    done
    sips -z 1024 1024 "$ICON_DIR/bolo-icon-1024.png" \
        --out "$ICON_DIR/Bolo.iconset/icon_512x512@2x.png" >/dev/null \
        || fail "icon resize failed"
    iconutil -c icns -o "$ICON_DIR/Bolo.icns" "$ICON_DIR/Bolo.iconset" \
        || fail "iconutil could not build Bolo.icns"
fi
[ -f "$ICON_DIR/Bolo.icns" ] || fail "Bolo.icns missing"

# --- Phase 5: stage Bolo.app ----------------------------------------------------------

STAGE="$BUILD/staging.noindex"
rm -rf "$STAGE"
APP="$STAGE/Bolo.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

log "staging the app bundle"
cp "$BUILD/universal/bolo" "$APP/Contents/MacOS/bolo-runtime"
chmod +x "$APP/Contents/MacOS/bolo-runtime"
install -m 755 "$ROOT/scripts/bundle-launcher.sh" "$APP/Contents/MacOS/bolo"

for helper in hotkey.py accessibility_daemon.py overlay.py insert_text.py \
    accessibility_context.py accessibility_trusted.py app_window.py \
    onboarding.py bolo_env.py ensure-python-env.sh helper-requirements.txt \
    vocabulary.json; do
    cp "$ROOT/$helper" "$APP/Contents/Resources/" || fail "missing bundle helper: $helper"
done
chmod +x "$APP/Contents/Resources/ensure-python-env.sh"

mkdir -p "$APP/Contents/Resources/python" "$APP/Contents/Resources/wheels"
cp -R "$PYBUILD/aarch64/python" "$APP/Contents/Resources/python/aarch64"
cp -R "$PYBUILD/x86_64/python" "$APP/Contents/Resources/python/x86_64"
cp "$WHEELS"/*.whl "$APP/Contents/Resources/wheels/"
cp "$ICON_DIR/Bolo.icns" "$APP/Contents/Resources/Bolo.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Bolo</string>
    <key>CFBundleDisplayName</key>
    <string>Bolo</string>
    <key>CFBundleExecutable</key>
    <string>bolo</string>
    <key>CFBundleIconFile</key>
    <string>Bolo</string>
    <key>CFBundleIdentifier</key>
    <string>com.a692570.bolo</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundleVersion</key>
    <string>$VERSION</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Bolo records your microphone while you hold the dictation key and stops the moment you let go.</string>
    <key>NSAccessibilityUsageDescription</key>
    <string>Bolo uses Accessibility to paste dictated text into the app you are working in.</string>
    <key>NSSupportsSuddenTermination</key>
    <false/>
</dict>
</plist>
PLIST

ln -sfn /Applications "$STAGE/Applications"

# --- Phase 6: ad-hoc codesign ----------------------------------------------------------

log "ad-hoc code signing the bundle"
codesign --force --deep --sign - "$APP" || fail "ad-hoc codesign failed"

# --- Phase 6.5: Finder presentation (background + icon layout) --------------------------

# The volume ships a Finder layout: a dark brand background drawn by
# scripts/make-dmg-background.py (cached in build/dmg, regenerated only when
# missing), the Applications symlink on the left and Bolo.app on the right.
# The layout is produced once on a read-write scratch volume whose .DS_Store
# is copied back into the staging root, so the final DMG mounts already
# arranged.
DMG_ASSETS="$BUILD/dmg"
BG="$DMG_ASSETS/background.png"
if [ ! -f "$BG" ]; then
    mkdir -p "$DMG_ASSETS"
    log "rendering the DMG background"
    "$ICON_VENV/bin/python3" "$ROOT/scripts/make-dmg-background.py" --output "$BG" \
        || fail "DMG background render failed"
fi
[ -f "$BG" ] || fail "DMG background missing"
mkdir -p "$STAGE/.background"
cp "$BG" "$STAGE/.background/background.png"

log "laying out the volume in Finder"
LAYOUT_DMG="$BUILD/staging-layout.dmg"
rm -f "$LAYOUT_DMG"
if [ -e /Volumes/Bolo ]; then
    fail "a volume named Bolo is already mounted at /Volumes/Bolo; eject it and rebuild"
fi
hdiutil create -volname Bolo -format UDRW -srcfolder "$STAGE" "$LAYOUT_DMG" \
    >/dev/null || fail "could not create the layout DMG"
# The layout volume mounts at /Volumes/Bolo: Finder's AppleScript can only
# address disks at their standard mount points, so no -mountpoint here.
hdiutil attach "$LAYOUT_DMG" >/dev/null || fail "could not mount the layout DMG"
osascript <<APPLESCRIPT || fail "Finder layout failed"
tell application "Finder"
    delay 1
    tell disk "Bolo"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {0, 0, 660, 400}
        set theViewOptions to the icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 80
        set background picture of theViewOptions to file ".background:background.png"
        try
            set position of item "Bolo.app" to {480, 220}
        on error
            set position of item "Bolo" to {480, 220}
        end try
        set position of item "Applications" to {180, 220}
        update without registering applications
        delay 2
        close
    end tell
end tell
APPLESCRIPT
[ -e /Volumes/Bolo/.DS_Store ] || fail "Finder produced no .DS_Store"
[ -s /Volumes/Bolo/.DS_Store ] || fail "Finder produced an empty .DS_Store"
strings /Volumes/Bolo/.DS_Store | grep -q "icvp" \
    || fail "Finder layout did not write icon view options into the .DS_Store"
strings /Volumes/Bolo/.DS_Store | grep -q "background.png" \
    || fail "Finder layout does not reference the background image"
cp /Volumes/Bolo/.DS_Store "$STAGE/.DS_Store" \
    || fail "could not copy the .DS_Store into staging"
detached=false
for _ in 1 2 3; do
    if hdiutil detach /Volumes/Bolo >/dev/null 2>&1; then
        detached=true
        break
    fi
    sleep 2
done
[ "$detached" = true ] || fail "could not detach the layout DMG"
rm -f "$LAYOUT_DMG"

# --- Phase 7: DMG ------------------------------------------------------------------------

DMG="$DIST/Bolo-$VERSION.dmg"
rm -f "$DMG"
log "creating $DMG"
# LZMA-compressed DMG: roughly half the size of the classic UDZO image,
# which matters for a 90MB artifact on flaky home networks. Readable on
# macOS 10.15+, the minimum version the Info.plist already requires.
hdiutil create -volname Bolo -srcfolder "$STAGE" -format ULMO -ov "$DMG" \
    >/dev/null || fail "hdiutil could not create the DMG"
[ -f "$DMG" ] || fail "DMG missing after hdiutil"

# --- Phase 8: verify -----------------------------------------------------------------------

SIZE_BYTES="$(stat -f %z "$DMG")"
SIZE_MB=$(((SIZE_BYTES + 512 * 1024 - 1) / (1024 * 1024)))
log "DMG size: ${SIZE_MB}MB"
if [ "$SIZE_MB" -gt 150 ]; then
    log "WARNING: DMG exceeds the 150MB target; consider trimming the bundled runtimes"
fi

MOUNT="$BUILD/dmg-mount"
rm -rf "$MOUNT"
mkdir -p "$MOUNT"
hdiutil attach -nobrowse -readonly "$DMG" -mountpoint "$MOUNT" >/dev/null \
    || fail "could not mount the fresh DMG"
trap 'hdiutil detach "$MOUNT" >/dev/null 2>&1 || true' EXIT

MOUNTED_APP="$MOUNT/Bolo.app"
for expected in \
    "Contents/Info.plist" \
    "Contents/MacOS/bolo" \
    "Contents/MacOS/bolo-runtime" \
    "Contents/Resources/app_window.py" \
    "Contents/Resources/ensure-python-env.sh" \
    "Contents/Resources/helper-requirements.txt" \
    "Contents/Resources/Bolo.icns" \
    "Contents/Resources/python/aarch64/bin/python3.12" \
    "Contents/Resources/python/x86_64/bin/python3.12" \
    "Contents/Resources/wheels"; do
    [ -e "$MOUNTED_APP/$expected" ] || fail "mounted bundle is missing $expected"
done
MOUNTED_ARCHS="$(lipo -archs "$MOUNTED_APP/Contents/MacOS/bolo-runtime")"
[ "$MOUNTED_ARCHS" = "x86_64 arm64" ] || [ "$MOUNTED_ARCHS" = "arm64 x86_64" ] \
    || fail "mounted binary is not universal (got: $MOUNTED_ARCHS)"
[ -e "$MOUNT/Applications" ] || fail "DMG is missing the /Applications symlink"
[ -e "$MOUNT/.background/background.png" ] \
    || fail "DMG is missing the Finder background"
[ -s "$MOUNT/.DS_Store" ] || fail "DMG is missing the Finder layout (.DS_Store)"
codesign --verify --deep "$MOUNTED_APP" >/dev/null 2>&1 \
    || fail "mounted bundle fails codesign verification"
WHEEL_COUNT="$(find "$MOUNTED_APP/Contents/Resources/wheels" -name '*.whl' | wc -l | tr -d ' ')"
[ "$WHEEL_COUNT" -ge 5 ] || fail "expected at least 5 wheels in the bundle, found $WHEEL_COUNT"

hdiutil detach "$MOUNT" >/dev/null || true
trap - EXIT

log "done: $DMG (${SIZE_MB}MB, universal $MOUNTED_ARCHS)"

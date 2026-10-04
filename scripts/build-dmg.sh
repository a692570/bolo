#!/bin/bash
# Build a distributable Bolo DMG in one shot.
#
# Phases (each logs a [dmg] marker and fails loudly):
#   1. universal Rust binary (aarch64 via the system cargo; x86_64 via a
#      rustup toolchain bootstrapped inside build/ when the system rust has
#      no cross-target std, as with Homebrew rust)
#   2. bundled python-build-standalone runtimes, pinned by URL + SHA256
#   3. pyobjc wheels (resolved from helper-requirements.txt, cached)
#   4. app icon (scripts/make_icon.py under a build-time venv)
#   5. Bolo.app staging (launcher, binary, helpers, runtimes, wheels, plist)
#   6. codesign: ad-hoc by default (preview); --release requires a real
#      Developer ID identity and the notary profile up front
#   6.5 Finder presentation: background image + programmatic .DS_Store
#       (scripts/make-dmg-layout.py with the pinned ds_store/mac_alias
#       build deps) written on a read-write scratch volume mounted at
#       /Volumes/Bolo so the background alias is valid, then verified
#   7. DMG creation with an /Applications symlink and the Finder layout
#   8. verification: mount, structure, universal binary, codesign, size
#
# By default the build produces dist/Bolo-<version>-preview.dmg, labeled
# as an ad-hoc-signed preview; the previously released
# dist/Bolo-<version>.dmg is never overwritten. Pass --release for the
# production name after the preflight below passes.
#
# Artifacts land in build/ (cached across runs). The user-facing install
# flow is: open the DMG, drag Bolo to Applications, launch; the bundle
# creates ~/.bolo/bundle-venv offline from the bundled wheels and collects the
# AssemblyAI key in the onboarding window.

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

RELEASE=0
for arg in "$@"; do
    case "$arg" in
        --release) RELEASE=1 ;;
        *) echo "[dmg] ERROR: unknown option: $arg" >&2; exit 1 ;;
    esac
done

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

# --- Release preflight: fail fast, before any lengthy build --------------------
# Release signing needs a real Developer ID Application identity plus a
# stored notarytool profile. Without both, refuse early instead of after a
# multi-minute build. This path is documented, not live-verified here:
# no Developer ID identity was available when this preflight was written.

if [ "$RELEASE" = 1 ]; then
    log "release mode: running the signing/notarization preflight"
    CODESIGN_IDENTITY="${BOLO_CODESIGN_IDENTITY:-}"
    NOTARY_PROFILE="${BOLO_NOTARY_PROFILE:-}"
    [ -n "$CODESIGN_IDENTITY" ] \
        || fail "--release needs BOLO_CODESIGN_IDENTITY (Developer ID Application: Name (TEAMID))"
    [ -n "$NOTARY_PROFILE" ] \
        || fail "--release needs BOLO_NOTARY_PROFILE (a stored notarytool profile: xcrun notarytool store-credentials)"
    # Only a Developer ID Application identity qualifies. Apple Development
    # and other non-distributable identities listed by find-identity must
    # be rejected here, before the build, not after.
    matching_identities="$(security find-identity -v -p codesigning 2>/dev/null | grep -F "$CODESIGN_IDENTITY" || true)"
    [ -n "$matching_identities" ] \
        || fail "no valid codesigning identity matches BOLO_CODESIGN_IDENTITY ('$CODESIGN_IDENTITY')"
    echo "$matching_identities" | grep -F "Developer ID Application" >/dev/null \
        || fail "BOLO_CODESIGN_IDENTITY must be a Developer ID Application identity, got: $CODESIGN_IDENTITY"
    xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1 \
        || fail "notarytool profile '$NOTARY_PROFILE' is not usable (store it with: xcrun notarytool store-credentials)"
    log "release preflight passed: Developer ID identity + notary profile present"
else
    log "preview mode: the bundle will be ad-hoc signed, never notarized"
fi

mkdir -p "$BUILD" "$DIST" "$DOWNLOADS" "$WHEELS" "$ICON_DIR" "$PYBUILD"

# python-build-standalone CPython 3.12.15, release 20261001 (pinned).
PBS_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/20261001"
PBS_AARCH64_URL="$PBS_BASE/cpython-3.12.15%2B20261001-aarch64-apple-darwin-install_only.tar.gz"
PBS_AARCH64_SHA="f1ee170bd7bb45bea526c4f9489b41f9c5d978c4cd7a08fd11809de56c39b736"
PBS_X86_64_URL="$PBS_BASE/cpython-3.12.15%2B20261001-x86_64-apple-darwin-install_only.tar.gz"
PBS_X86_64_SHA="f350524f5b80b9e1aeb25e39570e59f75edb6926582dc8f6edf2d0c744a7997c"

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

# DMG layout writer deps (ds_store + mac_alias), pinned in
# scripts/dmg-requirements.txt; installed into the icon venv, never shipped.
if ! "$ICON_VENV/bin/python3" -c 'import ds_store, mac_alias' >/dev/null 2>&1; then
    log "installing the pinned .DS_Store writer deps"
    "$ICON_VENV/bin/python3" -m pip install --no-input --disable-pip-version-check \
        --require-hashes -r "$ROOT/scripts/dmg-requirements.txt" >/dev/null \
        || fail "ds_store/mac_alias install failed"
fi
"$ICON_VENV/bin/python3" -c 'import ds_store, mac_alias' >/dev/null 2>&1 \
    || fail ".DS_Store writer deps missing after install"

# --- Phase 4: app icon ---------------------------------------------------------------

ICON_SOURCES=("$ROOT/scripts/make_icon.py" "$ROOT/bolo_brand.py" "$ROOT/scripts/make-dmg-background.py")
ICON_DIGEST="$(shasum -a 256 "${ICON_SOURCES[@]}" | awk '{print $1}' | shasum -a 256 | awk '{print $1}')"
ICON_MARKER="$ICON_DIR/.source-${ICON_DIGEST}"
# An icns only counts as fresh when its source digest matches. The
# presence of Bolo.icns alone used to skip source edits, leaving stale
# pixels when bolo_brand or the icon script changed.
if [ -f "$ICON_DIR/Bolo.icns" ] && [ -f "$ICON_MARKER" ]; then
    log "Bolo.icns matches the current icon source digest; reusing $ICON_DIR/Bolo.icns"
else
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
    # Refresh the marker, drop any prior markers.
    find "$ICON_DIR" -maxdepth 1 -name '.source-*' -delete >/dev/null 2>&1 || true
    : > "$ICON_MARKER"
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
# The main executable is the small native launcher (Swift, compiled
# universal below); the shell keeps its supervision work as a Resources
# helper. This keeps CFBundleExecutable a Mach-O binary, so the app
# presents a native process identity while the runtime runs.
install -m 755 "$ROOT/scripts/bundle-launcher.sh" "$APP/Contents/Resources/bolo-helper"
LAUNCHER_BUILD="$BUILD/native-launcher"
rm -rf "$LAUNCHER_BUILD"
mkdir -p "$LAUNCHER_BUILD"
for arch_target in arm64-apple-macos12.0 x86_64-apple-macos12.0; do
    log "compiling the native launcher for $arch_target"
    swiftc -target "$arch_target" -O -swift-version 5 \
        -disable-autolinking-runtime-compatibility \
        -disable-autolinking-runtime-compatibility-concurrency \
        -disable-autolinking-runtime-compatibility-dynamic-replacements \
        -o "$LAUNCHER_BUILD/bolo-${arch_target%%-*}" \
        "$ROOT/scripts/bundle-launcher.swift" \
        || fail "native launcher compile failed for $arch_target"
done
lipo -create \
    "$LAUNCHER_BUILD/bolo-arm64" "$LAUNCHER_BUILD/bolo-x86_64" \
    -output "$LAUNCHER_BUILD/bolo" \
    || fail "native launcher lipo failed"
install -m 755 "$LAUNCHER_BUILD/bolo" "$APP/Contents/MacOS/bolo"
lipo -archs "$APP/Contents/MacOS/bolo" | grep -q arm64 \
    || fail "native launcher is missing arm64"
lipo -archs "$APP/Contents/MacOS/bolo" | grep -q x86_64 \
    || fail "native launcher is missing x86_64"

for helper in hotkey.py accessibility_daemon.py overlay.py insert_text.py \
    accessibility_context.py accessibility_trusted.py app_window.py \
    dashboard_window.py onboarding.py bolo_env.py bolo_brand.py \
    ensure-python-env.sh helper-requirements.txt vocabulary.json; do
    cp "$ROOT/$helper" "$APP/Contents/Resources/" || fail "missing bundle helper: $helper"
done
cp "$ROOT/Third-Party Notices.txt" "$APP/Contents/Resources/Third-Party Notices.txt" \
    || fail "missing third-party notices"
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

# --- Phase 6: codesign -----------------------------------------------------------

if [ "$RELEASE" = 1 ]; then
    log "signing the bundle for release with '$CODESIGN_IDENTITY'"
    # Nested Mach-O files first (bottom-up, as Apple's docs require). The
    # candidates are found by their Mach-O magic, not by filename: fat
    # archives (cafebabe), 64-bit (cffaedfe) and 32-bit (cefaedfe) files.
    # Symlinks are excluded so nothing is signed twice, and the main
    # executable (Contents/MacOS/bolo-runtime) is signed before the bundle.
    macho_candidates() {
        # Mach-O magic check: fat archives (cafebebe), 64-bit (cffaedfe)
        # and 32-bit (cefaedfe). Symlinks excluded (find -type f).
        find "$APP/Contents" -type f -print0 | while IFS= read -r -d '' candidate; do
            magic="$(xxd -p -l 4 "$candidate" 2>/dev/null || true)"
            case "$magic" in
                cafebebe|cffaedfe|cefaedfe) printf '%s\0' "$candidate" ;;
            esac
        done
    }
    while IFS= read -r -d '' nested; do
        codesign --force --options runtime --timestamp \
            --sign "$CODESIGN_IDENTITY" "$nested" \
            || fail "nested Mach-O signing failed for $nested"
    done < <(macho_candidates)
    nested_count="$(macho_candidates | xargs -0 -n1 printf '%s\n' | wc -l | tr -d ' ')"
    log "signed $nested_count nested Mach-O files bottom-up"
    codesign --force --options runtime --timestamp \
        --entitlements "$ROOT/entitlements.plist" \
        --sign "$CODESIGN_IDENTITY" "$APP" \
        || fail "app bundle signing failed"
    codesign --verify --strict "$APP" >/dev/null \
        || fail "signed bundle fails verification"
else
    log "ad-hoc code signing the bundle (preview build, never notarized)"
    codesign --force --deep --sign - "$APP" || fail "ad-hoc codesign failed"
fi

# --- Phase 6.5: Finder presentation (background + icon layout) --------------------------

# The volume ships a Finder layout: a light neutral installer background
# drawn by scripts/make-dmg-background.py (cached in build/dmg, re-rendered
# whenever the source script is newer than the cached image), Bolo.app on
# the left and the Applications symlink on the right. The .DS_Store is
# written programmatically by scripts/make-dmg-layout.py directly on the
# read-write layout volume, and that SAME volume is then converted into
# the final image, so the volume UUID and CNIDs the background alias and
# icon slots reference are the ones the shipped DMG presents. The
# background file is named by its content hash so a changed image is a
# changed URL and Finder's image cache cannot serve a stale picture for
# the same path. No Finder AppleScript, no UI automation.
DMG_ASSETS="$BUILD/dmg"
BG="$DMG_ASSETS/background.png"
mkdir -p "$DMG_ASSETS"
if [ ! -f "$BG" ] || [ "$ROOT/scripts/make-dmg-background.py" -nt "$BG" ]; then
    log "rendering the DMG background"
    "$ICON_VENV/bin/python3" "$ROOT/scripts/make-dmg-background.py" --output "$BG" \
        || fail "DMG background render failed"
fi
[ -f "$BG" ] || fail "DMG background missing"
BG_SHA="$(shasum -a 256 "$BG" | awk '{print $1}')"
[ -n "$BG_SHA" ] || fail "could not hash the DMG background"
BG_NAME="background-${BG_SHA}.png"
log "background content hash: $BG_SHA"
mkdir -p "$STAGE/.background"
cp "$BG" "$STAGE/.background/$BG_NAME"

if [ -e /Volumes/Bolo ]; then
    fail "a volume named Bolo is already mounted at /Volumes/Bolo; eject it and rebuild"
fi
LAYOUT_DMG="$BUILD/staging-layout.dmg"
rm -f "$LAYOUT_DMG"
hdiutil create -volname Bolo -fs HFS+ -format UDRW -srcfolder "$STAGE" "$LAYOUT_DMG" \
    >/dev/null || fail "could not create the layout DMG"
# The layout volume must mount at /Volumes/Bolo: the background alias
# embedded in the .DS_Store resolves against that volume root.
hdiutil attach "$LAYOUT_DMG" >/dev/null || fail "could not mount the layout DMG"
[ -d /Volumes/Bolo ] || fail "layout volume did not mount at /Volumes/Bolo"
[ -f "/Volumes/Bolo/.background/$BG_NAME" ] \
    || fail "the hashed background did not reach the layout volume"

detached=false
cleanup_layout() {
    if [ "$detached" = false ] && [ -d /Volumes/Bolo ]; then
        hdiutil detach /Volumes/Bolo >/dev/null 2>&1 || true
    fi
    rm -f "$LAYOUT_DMG"
}
trap cleanup_layout EXIT

log "writing the Finder layout programmatically"
"$ICON_VENV/bin/python3" "$ROOT/scripts/make-dmg-layout.py" \
    --volume /Volumes/Bolo \
    --background ".background/$BG_NAME" \
    --bolo-position 150,232 --applications-position 462,232 \
    --window 0,0,660,400 --verify \
    || fail "could not write or verify the .DS_Store layout"
[ -s /Volumes/Bolo/.DS_Store ] || fail "the layout volume has no .DS_Store"

for _ in 1 2 3; do
    if hdiutil detach /Volumes/Bolo >/dev/null 2>&1; then
        detached=true
        break
    fi
    sleep 2
done
[ "$detached" = true ] || fail "could not detach the layout DMG"
# Do NOT delete the layout image: the final DMG is converted from it so
# the volume UUID and CNIDs the alias refers to are preserved.
trap - EXIT

# --- Phase 7: DMG ------------------------------------------------------------------------

if [ "$RELEASE" = 1 ]; then
    DMG="$DIST/Bolo-$VERSION.dmg"
else
    DMG="$DIST/Bolo-$VERSION-preview.dmg"
fi
# Never touch an already-released artifact, even in release mode.
if [ -e "$DMG" ]; then
    if [ "$RELEASE" = 1 ]; then
        fail "$DMG already exists; move or delete it before rebuilding the release"
    fi
    log "removing the old preview artifact $DMG"
    rm -f "$DMG"
fi
log "creating $DMG"
# Convert the SAME layout image that carries the .DS_Store into the
# compressed artifact, rather than recreating an image from the staging
# folder: a fresh volume would have a different UUID and CNIDs, breaking
# the alias the layout embedded. UDZO instead of ULMO keeps hdiutil
# convert happy; the size difference is small for this artifact.
hdiutil convert "$LAYOUT_DMG" -format UDZO -ov -o "$DMG" \
    >/dev/null || fail "hdiutil could not convert the layout DMG"
rm -f "$LAYOUT_DMG"
[ -f "$DMG" ] || fail "DMG missing after hdiutil"

# --- Phase 8: verify -----------------------------------------------------------------------

SIZE_BYTES="$(stat -f %z "$DMG")"
SIZE_MB=$(((SIZE_BYTES + 512 * 1024 - 1) / (1024 * 1024)))
log "DMG size: ${SIZE_MB}MB"
if [ "$SIZE_MB" -gt 150 ]; then
    log "WARNING: DMG exceeds the 150MB target; consider trimming the bundled runtimes"
fi

MOUNT="$BUILD/dmg-mount"
# A failed verification may leave our read-only image attached. Detach
# it before clearing the directory; never recurse into a mounted image.
detach_verify() {
    if mount | grep -Fq " on $MOUNT ("; then
        for attempt in 1 2 3; do
            hdiutil detach "$MOUNT" >/dev/null 2>&1 && return 0
            sleep 1
        done
        hdiutil detach -force "$MOUNT" >/dev/null 2>&1 || return 1
    fi
}
detach_verify || fail "could not detach the previous verification image"
rm -rf "$MOUNT"
mkdir -p "$MOUNT"
hdiutil attach -nobrowse -readonly "$DMG" -mountpoint "$MOUNT" >/dev/null \
    || fail "could not mount the fresh DMG"
trap 'detach_verify || true' EXIT

MOUNTED_APP="$MOUNT/Bolo.app"
for expected in \
    "Contents/Info.plist" \
    "Contents/MacOS/bolo" \
    "Contents/MacOS/bolo-runtime" \
    "Contents/Resources/bolo-helper" \
    "Contents/Resources/app_window.py" \
    "Contents/Resources/dashboard_window.py" \
    "Contents/Resources/bolo_brand.py" \
    "Contents/Resources/Third-Party Notices.txt" \
    "Contents/Resources/ensure-python-env.sh" \
    "Contents/Resources/helper-requirements.txt" \
    "Contents/Resources/Bolo.icns" \
    "Contents/Resources/python/aarch64/bin/python3.12" \
    "Contents/Resources/python/x86_64/bin/python3.12" \
    "Contents/Resources/wheels"; do
    [ -e "$MOUNTED_APP/$expected" ] || fail "mounted bundle is missing $expected"
done
MOUNTED_LAUNCHER_ARCHS="$(lipo -archs "$MOUNTED_APP/Contents/MacOS/bolo")"
[ "$MOUNTED_LAUNCHER_ARCHS" = "x86_64 arm64" ] || [ "$MOUNTED_LAUNCHER_ARCHS" = "arm64 x86_64" ] \
    || fail "native launcher is not universal (got: $MOUNTED_LAUNCHER_ARCHS)"
[ -x "$MOUNTED_APP/Contents/MacOS/bolo" ] \
    || fail "native launcher is not executable"
[ -x "$MOUNTED_APP/Contents/Resources/bolo-helper" ] \
    || fail "the shell helper is not executable"
MOUNTED_ARCHS="$(lipo -archs "$MOUNTED_APP/Contents/MacOS/bolo-runtime")"
[ "$MOUNTED_ARCHS" = "x86_64 arm64" ] || [ "$MOUNTED_ARCHS" = "arm64 x86_64" ] \
    || fail "mounted binary is not universal (got: $MOUNTED_ARCHS)"
[ -e "$MOUNT/Applications" ] || fail "DMG is missing the /Applications symlink"
[ -e "$MOUNT/.background/$BG_NAME" ] \
    || fail "DMG is missing the hashed Finder background"
[ -s "$MOUNT/.DS_Store" ] || fail "DMG is missing the Finder layout (.DS_Store)"
# Read the shipped .DS_Store back and prove its background alias resolves
# to the hashed background inside THIS volume: a rebuilt-from-folder image
# would carry an alias pointing at a different volume identity.
if ! "$ICON_VENV/bin/python3" "$ROOT/scripts/make-dmg-layout.py" \
    --volume "$MOUNT" --verify-existing; then
    fail "the mounted DMG's .DS_Store background alias does not resolve in-volume"
fi
codesign --verify --deep "$MOUNTED_APP" >/dev/null 2>&1 \
    || fail "mounted bundle fails codesign verification"
WHEEL_COUNT="$(find "$MOUNTED_APP/Contents/Resources/wheels" -name '*.whl' | wc -l | tr -d ' ')"
[ "$WHEEL_COUNT" -ge 5 ] || fail "expected at least 5 wheels in the bundle, found $WHEEL_COUNT"

detach_verify || fail "could not detach the verification image"
trap - EXIT

# --- Phase 8: release-only: notarize the DMG, staple, validate -----------------------
# Documented against Apple's notarization docs:
#   https://developer.apple.com/help/account/reference/codesigning-notarization
# and the notarytool Customizing the notarization workflow doc
#   https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution/customizing-the-notarization-workflow
# NOT live verified here: no Developer ID identity is available and no
# notary profile was configured for this build, so this branch has never
# been exercised end to end. Do not call the output production verified
# until a real identity runs the full path on the final bundle.

if [ "$RELEASE" = 1 ]; then
    log "notarizing $DMG with profile '$NOTARY_PROFILE' (this can take minutes)"
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait \
        || fail "notarytool submission failed"
    xcrun stapler staple "$DMG" || fail "stapler failed"
    xcrun stapler validate "$DMG" || fail "stapled DMG fails validation"
    spctl -a -t open --context context:primary-signature -v "$DMG" \
        || fail "Gatekeeper assessment rejected the DMG"
    log "notarized and stapled: $DMG"
else
    log "preview build: ad-hoc signed, NOT notarized; label it as a preview"
fi

if [ "$RELEASE" = 1 ]; then
    log "done: $DMG (${SIZE_MB}MB, universal $MOUNTED_ARCHS, Developer ID signed + notarized)"
else
    log "done: $DMG (${SIZE_MB}MB, universal $MOUNTED_ARCHS, ad-hoc preview, not notarized)"
fi

# Releasing Bolo

Build and sign with the standard Apple toolchain. Primary references: Developer ID signing and notarization overview at https://developer.apple.com/help/account/reference/codesigning-notarization and customizing the notarization workflow at https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution/customizing-the-notarization-workflow

The default preview build creates a local DMG. Release mode adds Developer ID signing and notarization. The native launcher keeps the app running, opens the dashboard on an interactive launch, and forwards quits to the runtime. This document also covers the installer layout, signature checks, and bundled notices.

## Preview build (default)

```
scripts/build-dmg.sh
```

Produces `dist/Bolo-<version>-preview.dmg` (using the version from `Cargo.toml`). The bundle is signed with an ad-hoc signature and is never notarized. The log says this explicitly on every run. An existing `dist/Bolo-<version>.dmg` (a previously released artifact) is never touched by a preview build; only an older preview file of the same name is replaced.

This is a local test artifact. Whether it opens without warnings on a different Mac depends on that machine's Gatekeeper policy and local security settings, which this build cannot promise. Treat it as a build for your own machine and for sharing with teammates who know what it is.

## Release build

```
scripts/build-dmg.sh --release
```

Requirements, all checked before any lengthy build phase:

- `BOLO_CODESIGN_IDENTITY` must name a valid **Developer ID Application** identity in the keychain. Apple Development or any other identity type is rejected, even when `security find-identity` lists it.
- `BOLO_NOTARY_PROFILE` must name a stored notarytool profile (`xcrun notarytool store-credentials`).
- The release artifact is `dist/Bolo-<version>.dmg`. The build refuses to overwrite an existing file with that name; move or delete it first.

What the release mode does, in order:

1. Signs every nested Mach-O file bottom-up. Candidates are found by their Mach-O magic (fat archives, 64-bit and 32-bit headers), not by filename, so the Rust runtime `Contents/MacOS/bolo-runtime` and the bundled Python executables and dylibs are all included. Symlinks are skipped so nothing is signed twice. Each is signed with hardened runtime and a secure timestamp.
2. Signs the app bundle with `entitlements.plist`.
3. Verifies the signed bundle strictly.
4. Builds the DMG, mounts it, and verifies structure, universal binary, code signature, and wheel count.
5. Notarizes the DMG with `notarytool submit --wait`, then staples it and validates the staple.

## Release path is NOT live verified

Read this before trusting a release build:

- No Developer ID Application identity was available to this build, and no notarytool profile was configured for it, so the release path has never been exercised end to end. Only its rejection paths (the preflight failures) are covered by tests.
- The main executable, `Contents/MacOS/bolo`, is a native Mach-O launcher (compiled from `scripts/bundle-launcher.swift` for arm64 and x86_64, minimum macOS 12.0, universal via lipo). It runs the shell helper (`Contents/Resources/bolo-helper`) in foreground mode. The launcher is an accessory without a Dock icon and owns no windows. An interactive launch or reopen writes `~/.bolo/open-dashboard.request`; the runtime consumes it and opens or activates the dashboard after onboarding. Login-item launches stay quiet.
- The native launcher keeps a native process identity alive while the runtime runs, but that alone does not constitute a verified trust, Gatekeeper, or helper-attribution result. Do not describe any output of `--release` as production verified until a real identity has run the full signing and notarization path on the final bundle and the result has been checked on a clean machine.

## Corrupted bundles are never repaired

The helper does not re-sign a bundle that fails `codesign --verify`. A bundle whose signature does not verify exits with a notification telling the user to reinstall from the latest DMG. The check runs before the supervisor lock is taken. The offline venv may already exist; it is a user-level cache. The runtime is supervised with an explicit child PID. A quit signal is forwarded to that child and waited on. Signals received during startup are recorded until cleanup is installed. Global lock cleanup requires both the supervisor and runtime PID files to name the current processes.

## Finder presentation

The runtime builds the menu and dashboard from cached microphone facts. A single background scan updates device names and the default input UID. A slow scan leaves the UI responsive and retains the last completed device list. The microphone menu says it is looking for devices before the first successful scan. System Default and saved UID recording choices resolve directly before any legacy-name enumeration.

The DMG window is 660x400 with Bolo.app on the left and the Applications symlink on the right, with a rightward drag arrow on the background between the two icons. The background and the `.DS_Store` are generated without Finder AppleScript or any UI automation:

- `scripts/make-dmg-background.py` renders a light neutral installer surface with a dark charcoal wordmark and instructions and the clay brand accent, so the icon labels Finder draws stay readable. It runs offscreen under the build-time venv and re-renders whenever the script is newer than the cached image.
- The rendered PNG is staged under a content-hash filename (`.background/background-<sha256>.png`), so a changed image is a changed URL and Finder's image cache cannot serve a stale picture for the same path.
- `scripts/make-dmg-layout.py` writes the `.DS_Store` with `ds_store` and references the background through a `mac_alias` alias, the same mechanism dmgbuild uses. Both packages are build-only dependencies, pinned with hashes verified against PyPI's official JSON metadata in `scripts/dmg-requirements.txt`, installed into the build venv and never shipped.
- The layout is written directly on the read-write layout volume mounted at `/Volumes/Bolo`, and the final DMG is converted from that same image rather than rebuilt from the staging folder, so the volume identity the alias references is the one the shipped DMG presents. The mounted artifact is then verified by resolving the alias inside the mounted volume.

## Evidence log

The checked evidence for the packaging work, including the native launcher compilation, the layout readback tests, and the list of what was not live verified, is recorded in `~/Documents/Codex/2026-10-01/u-x20/work/bolo-packaging-checks.log`.

## Third-party notices

The build copies `Third-Party Notices.txt` into `Bolo.app/Contents/Resources` and requires it when checking the mounted DMG. The notice includes OpenSuperWhisper's MIT license and the source revision used for Bolo's microphone and display-placement adaptations. Keep the notice with redistributed app bundles.

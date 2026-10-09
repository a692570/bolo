# Troubleshooting Bolo

Quick fixes for the problems real users hit. If nothing here helps, check the
logs (`/tmp/bolo.log`, or About Bolo from the menu bar) and file an issue with
the tail attached.

## Nothing pastes, or a notification says Bolo cannot paste

**Cause:** macOS Accessibility is off for Bolo. This permission never shows a
popup; it must be enabled by hand.
**Fix:** Open System Settings > Privacy & Security > Accessibility and enable
Bolo, then quit Bolo from its menu bar and open it again. The setup window's
Accessibility row does this for you (Open Accessibility Settings).

## Dictations come back empty, or the overlay says no speech

**Cause:** the microphone delivered silence. Bolo records what your input
device gives it.
**Fix:** Check the mic's mute switch and gain. Check macOS Sound > Input shows
your mic moving when you speak. To pin Bolo to a specific mic, use Settings >
Microphone in the dashboard (the picker writes the choice; Bolo follows the
system default when nothing is pinned).

## Setup says an API key is missing

**Cause:** Bolo cannot reach its transcription provider without a key.
**Fix:** In the setup window, pick your provider (AssemblyAI is recommended)
and paste the key from that provider's dashboard. AssemblyAI keys come from
https://www.assemblyai.com/dashboard/home. The key is validated before saving.

## Transcription fails with 401 Unauthorized

**Cause:** the key on disk is invalid, expired, or was regenerated.
**Fix:** Re-paste a fresh key in the setup window (it replaces the saved one).

## Transcription fails with 429 or rate limited

**Cause:** the provider's rate limit was hit (AssemblyAI's free tier allows 5
new streams per minute).
**Fix:** Wait a moment and dictate again; Bolo falls back to batch
transcription automatically.

## No live text in the overlay while recording

**Cause:** live preview rides a streaming connection; when it is off or cannot
connect, Bolo silently uses batch transcription (still works, slightly slower
to paste).
**Fix:** Nothing needed. If you want the preview, check Settings > streaming
and your connection.

## Bolo learned a wrong word

**Cause:** correction learning picks up your edits after dictation; a rephrase
can occasionally look like a correction.
**Fix:** Open Learned Words from the menu bar and tap the wrong pair to remove
it. Everything it has learned is listed there.

## Two Bolo copies fight over the microphone

**Cause:** a source install and the Bolo app cannot run at the same time (one
instance lock).
**Fix:** Quit one of them. If a stale copy lingers after uninstalling, run
`pkill -f Bolo` and relaunch.

## The app says its helper runtime is missing

**Cause:** this copy of Bolo is incomplete — usually a download or copy that
did not finish.
**Fix:** Download the latest DMG from the releases page and reinstall. The
app retries once before showing this, so it appearing means the copy is
genuinely incomplete.

## Source install: the update check says skipped

**Cause:** local changes in the repo make the updater refuse to overwrite them.
**Fix:** Intended for developers. Commit or stash your changes to let updates
apply.

## The hotkey does nothing

**Cause:** another app owns the key, or the app is not running (no icon in the
menu bar).
**Fix:** Check the menu bar for the Bolo icon. Try a different hotkey in
Settings > Dictation key. macOS may need a moment to re-check the physical key
state; pressing it again usually works.

## Where everything lives

- Logs: `/tmp/bolo.log` (also visible from About Bolo)
- Settings and keys: `~/.bolo/env` (edit via the dashboard, not by hand)
- Learned corrections: `~/.bolo/learned_vocabulary.json` (manage via Learned Words)
- Cleanup prompt overrides: `~/.bolo/cleanup_prompts.json` (manage via Cleanup prompts)

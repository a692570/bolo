# Bolo Design System

Principles for how Bolo communicates state to the user.

## Core principle

**Transparency over silence.** Every crash, timeout, invalid config, or unexpected state gets a clear, actionable overlay message. The user should never wonder "is Bolo running?" or "why didn't that work?"

## Overlay States

The recording overlay has these canonical states:

| State          | Color  | Icon | Meaning |
|----------------|--------|------|---------|
| `listening`    | green  | ●    | Recording, showing live partial transcripts |
| `transcribing` | white  | ○    | Processing audio, transcript not yet available |
| `inserting`    | blue   | →    | Typing text into app |
| `success`      | green  | ✓    | Text inserted successfully |
| `error`        | red    | ✗    | Something failed (with brief message) |
| `warning`      | yellow | ⚠    | Non-fatal issue (rate limited, etc.) |

## Error Messages

Every error message follows structure:

```
<what failed> — <actionable fix>
```

Bad: `"STT error"`
Good: `"Transcription failed — check your internet connection"`

Bad: `"401"`
Good: `"Invalid API key — edit ~/.bolo/env and restart"`

### Error Categories

1. **Config errors** (startup) — prevent launch. Show in terminal, log to file.
2. **STT errors** (mid-session) — show in overlay briefly, fall back to batch.
3. **Network errors** (mid-session) — show in overlay, enable backoff.
4. **Rate limiting** (mid-session) — show countdown in overlay.
5. **LLM errors** (async cleanup) — silent. Downgrade to raw text without cleanup.

## Logging

`/tmp/bolo.log` is the single log file. Format:

```
YYYY-MM-DD HH:MM:SS,SSS LEVEL [component] message
```

Levels: INFO (normal), WARN (recoverable), ERROR (failed but continuing), FATAL (crash).

Never log API keys or full request bodies.

## Menubar Communication

- Icon changes: idle → recording (active icon)
- Title: shows "⌥" when idle, changes to "..." during processing
- "Last transcript" menu item shows the most recent result
- "History" shows count of stored sessions

## Keyboard Feedthrough

When typing text, never simulate keystrokes for special characters that don't exist on US keyboard. Use the clipboard paste fallback for:
- emoji
- non-Latin scripts
- strings longer than 500 characters

## Timers and Watchdogs

All periodic timers must have explicit cleanup in `quit_app()`.
- `_process_key_events` — 0.02s
- `_watchdog_cg_tap` — 2s  
- `_watchdog_overlay_health` — 1s
- `_watchdog_overlay_preview` — 0.5s
- `_watchdog_recording` — 5s

## File Paths

All Bolo data lives under `~/.bolo/`:

```
~/.bolo/
  env              — API keys and config
  sessions.db      — SQLite dictation history
  corrections.json — learned corrections
  vocabulary.json  — user vocabulary overrides
  metrics.jsonl    — per-session timing metrics
  prefs.json       — toggles (auto-silence, clipboard mode)
```

## Non-Goals

- No Electron, no web UI. Bolo stays native Python + rumps.
- No analytics, no telemetry, no phoning home.
- No multi-window. Single overlay + menubar.

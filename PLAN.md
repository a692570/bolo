# Bolo Feature Enhancement Plan

## Current Architecture

```
Hotkey (Right Option) → NSEvent monitor → _process_key_events (20ms poll)
  ├── Press → _start_recording()
  │   ├── Claim warm STT stream or connect async
  │   ├── Open sounddevice InputStream
  │   ├── Play "Tink" sound
  │   └── Show overlay "listening"
  │
  ├── During recording:
  │   ├── _audio_callback → sends PCM to STT WebSocket
  │   ├── _drain_stream_transcripts → reads partials/finals
  │   ├── _handle_stream_transcript → overlay.update("listening", preview)
  │   └── _watchdog_overlay_preview → shows word count estimate if no partials
  │
  └── Release → _stop_recording()
      ├── Close audio stream
      ├── Send silence padding to STT
      ├── overlay.update("processing", "")
      └── _pipeline (background thread)
          ├── Race: streaming vs batch STT
          ├── Pick winner
          ├── _render_text → _type_text (CGEvent) / _delete_text
          ├── overlay.update("final", result)
          ├── Play "Pop" sound
          └── _hide_overlay_after_delay(0.9s)

Overlay: subprocess (overlay.py) → NSPanel floating window
  - Communication via stdin JSON: {"phase": "...", "text": "..."}
  - Phases: listening, processing, error, final
  - Controller: RecordingOverlay class (overlay_controller.py)
```

## Feature 1: Enhanced HUD States

### Changes to overlay.py
- Add "transcribing" phase: animated "Transcribing..." label
- Add "inserting" phase: animated "Inserting..." label
- Add "success" phase: brief green ✓ indicator before dismiss
- Update status label colors per phase (amber for transcribing, blue for inserting, green for success)

### Changes to bolo.py
- `_stop_recording()`: use "transcribing" instead of "processing"
- Before `_render_text()`: update overlay to "inserting"
- After successful injection: update overlay to "success" before auto-dismiss
- Keep existing audio cues (Tink/Pop) — HUD is additive

## Feature 2: Clipboard Paste Fallback

### New method: `_type_text_via_clipboard(text)`
1. Save current NSPasteboard contents
2. Set NSPasteboard to transcribed text
3. Simulate Cmd+V via CGEvent
4. Wait 50ms for paste to complete
5. Restore original clipboard contents
6. Log to /tmp/bolo.log

### Modified: `_type_text(text)`
- Existing CGEvent method (unchanged)

### New wrapper: `_inject_text(text, method="auto")`
- method="auto": try CGEvent, then clipboard fallback
- method="clipboard": force clipboard paste
- method="cgevent": force CGEvent only
- Auto-detection: check if frontmost app is in KNOWN_CLIPBOARD_APPS
- Log which method was used

### New: Menu toggle for "Clipboard paste mode"
- Persisted to ~/.bolo_prefs.json
- When enabled, always uses clipboard paste

## Feature 3: PROPOSAL.md (Swift rewrite assessment)
- Document only, no code changes

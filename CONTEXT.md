# CONTEXT.md

Domain glossary for Bolo. Names the concepts the code is organized around
so contributors and coding agents use one vocabulary. Covers the dictation
session lifecycle (hotkey, recording, transcript, cleanup, insertion),
the two runtimes, and the deep modules with their interfaces. Update this
file when a new concept gets a name or a fuzzy term gets sharpened.

## Runtimes

- **Rust runtime** (`src/main.rs`, built to `target/release/bolo`): the runtime `start-bolo.sh` launches. Spawns `hotkey.py`, `insert_text.py`, and `accessibility_context.py` as subprocess helpers.
- **Python runtime** (`bolo.py`): the rumps menu-bar app. Self-contained except for the modules below.

## Domain terms

- **Dictation session**: one hold-to-release cycle: hotkey press, recording, transcription, optional cleanup, insertion. Identified by a session id; stale sessions must not touch the screen.
- **Transcript**: text produced by STT. A **stream preview** arrives incrementally over the websocket; a **batch transcript** comes from a single HTTP request after release; the winner is chosen or **reconciled** by LLM.
- **Cleanup**: LLM pass that fixes punctuation, fillers, and app-specific tone before or after insertion.
- **Insertion**: putting transcript text into the focused app, via CGEvent keystrokes or clipboard paste with pasteboard restore.
- **Correction**: a follow-up dictation shortly after a paste that replaces the previous result ("scratch that", replace commands). Learned pairs persist in the correction store.
- **Vocabulary**: preferred terms (built-in + user) fed to STT prompts and cleanup.
- **Overlay**: the on-screen recording indicator, a subprocess driven over stdin.

## Deep modules

- **Inserter** (`inserter.py`): the insertion module. Interface: `render(previous, target)` (diff the visible text, delete the tail, inject the suffix, return what was injected), `inject(text)`, `delete(count)`. Strategy choice (CGEvent vs clipboard), the clipboard-app allow-list, and full-payload pasteboard snapshot/restore live inside. Shares the pasteboard helpers with `insert_text.py` so both runtimes use one implementation. Tests: `tests/test_inserter.py` via a recording fake.
- **Transcriber** (`transcriber.py`): the transcription module. Interface: `availability()`, `warm()`, `begin_session(context_text, on_partial)`; the session exposes `feed(pcm)`, `request_stop()`, `finish(wav, duration)` returning a two-phase result (`.preview` immediately for the command fast path, `.final()` for the stream-vs-batch race, `.cancel()` to shortcut). The warm pool, batch client with 429 fallback, chunked retry, race winner selection, STT backoff, and transcription timings all live inside. `stream_factory` and `batch_transport` are the test seam. STT and LLM rate limits are independent: an LLM 429 no longer blocks recording. Tests: `tests/test_transcriber.py` via FakeStream and a fake transport.

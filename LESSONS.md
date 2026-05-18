# Lessons

## Known Setup

- Bolo runs from `~/bolo` and starts through `start-bolo.command`.
- The current launcher builds `target/release/bolo` when needed and supervises the Rust runtime.
- Logs go to `/tmp/bolo.log`.

## Gotchas

- Do not assume old Python entrypoints are authoritative. The launcher stops older `bolo.py`, `hotkey.py`, and `overlay.py` processes before starting the Rust binary.
- macOS accessibility, microphone, and hotkey behavior should be verified after runtime changes.
- Keep STT model names aligned with the public Telnyx STT REST API docs. The default primary is `deepgram/nova-3`, and the rate-limit fallback is `openai/whisper-large-v3-turbo`.
- Do not hardcode experimental ASR models before they are available through the API Bolo actually calls. Expose model selection through `BOLO_STT_MODEL` and `BOLO_STT_FALLBACK_MODEL` so others can test new models without code changes.

## Verification Commands

```bash
./start-bolo.command
cargo build --release
tail -n 100 /tmp/bolo.log
```

## Do Not Break

- Keep the login-item launcher path stable unless the Login Item is updated too.
- Keep the lock files under `/tmp` coordinated with the launcher.

## Last Updated

2026-05-18

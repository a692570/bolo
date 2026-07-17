# Contributing

Thanks for contributing to Bolo.

## Before you start

Bolo is a macOS menubar app for push-to-talk dictation. Changes should be tested on macOS with both of these permissions enabled:

- Microphone
- Accessibility

You will also need a `TELNYX_API_KEY` to exercise the transcription flow.

## Development setup

1. Clone the repository.
2. Install Rust 1.88 or newer and Python 3.9 or newer.
3. Prepare the macOS helper environment:

```bash
./ensure-python-env.sh
```

For Python unit tests, use a separate development environment:

```bash
python3 -m venv .venv-dev
. .venv-dev/bin/activate
python -m pip install -r requirements.txt pytest
```

4. Export your API key:

```bash
export TELNYX_API_KEY="your_key_here"
```

5. Build and run the shipped Rust app:

```bash
cargo build --release
./start-bolo.sh
```

Run both test suites before opening a pull request:

```bash
cargo test --release
python -m pytest -q
```

Logs are written to:

```bash
/tmp/bolo.log
```

## What to work on

Useful contributions include:

- Bug fixes
- Better install and setup documentation
- Reduced transcription latency or better error handling
- UI and menu improvements
- Tests for parsing, correction logic, and utility code
- macOS permission and startup fixes

## Reporting bugs

Please open an issue with:

- What you expected
- What happened instead
- Steps to reproduce
- macOS version
- Python version
- Whether Microphone and Accessibility permissions were granted
- Relevant log lines from `/tmp/bolo.log` with secrets removed

Do not include API keys, tokens, or personal data.

## Pull requests

1. Fork the repo and create a focused branch.
2. Keep changes narrow and explain the reason for the change.
3. Update docs when behavior changes.
4. Test your change locally.
5. Open a pull request with a clear summary, screenshots if UI changed, and manual test notes.

Small, focused pull requests are easier to review than broad refactors.

## Style

- Keep dependencies minimal.
- Prefer simple changes over broad rewrites.
- Preserve existing behavior unless the pull request is intentionally changing it.
- Do not add telemetry, background recording, or behavior that weakens the push-to-talk privacy model without explicit discussion.

## Security

For security-sensitive issues, do not open a public issue first. Follow the process in `SECURITY.md`.

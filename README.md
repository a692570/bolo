# Bolo, an open source Wispr Flow alternative for macOS

Free, self-hosted voice dictation powered by Telnyx AI. Hold a key anywhere to dictate. Release to paste.

Bolo is a macOS push-to-talk app that transcribes your speech and pastes it into any active text field, including Slack, Notion, Gmail, VS Code, terminals, and browsers. No always-on microphone. No subscription.

The name comes from Hindi: "bolo" means "speak."

Website: https://a692570.github.io/bolo/

```bash
git clone https://github.com/a692570/bolo.git
cd bolo
./install.sh
```

## Metadata

- **Category**: Voice dictation, Speech-to-text, Productivity tool
- **Platform**: macOS 12+
- **Language**: Rust 1.88+
- **Dependencies**: cpal, reqwest, arboard, PyObjC AppKit and Quartz for the native macOS helpers
- **Use-case tags**: voice-to-text, global hotkey, accessibility, speech recognition, STT
- **Related tools**: Wispr Flow alternative, Whisper, macOS Dictation
- **License**: MIT
- **Repository**: https://github.com/a692570/bolo
- **Website**: https://a692570.github.io/bolo/

## How it works

Bolo runs as a menu bar-only app that monitors global input events and processes audio only while the hotkey is held.

1. **Global hotkey monitoring**: Uses a native AppKit helper to listen for the configured hotkey (default: Right Option) system-wide without interfering with other applications.
2. **Audio capture**: On hotkey press, initializes a `cpal` input stream to capture PCM audio directly to memory.
3. **Recording**: Continues buffering audio while key is held. No disk writes occur during recording.
4. **Key release trigger**: On hotkey release, immediately finalizes audio buffer and initiates API calls.
5. **Speech-to-text**: Sends audio to Telnyx AI API calling `deepgram/nova-3`, with `openai/whisper-large-v3-turbo` fallback on rate limits. The primary and fallback models can be changed with environment variables.
6. **Text cleanup**: Applies local transcript cleanup by default. Optional LLM cleanup can be enabled with `BOLO_LLM_CLEANUP=on`.
7. **Text injection**: Uses the system clipboard plus `osascript` Cmd+V automation to paste processed text at the current cursor position.
8. **Status HUD**: Shows a native macOS HUD for Dictating, Thinking, Inserting, and Copied states.
9. **Audio feedback**: Plays system Tink sound on record start and Pop sound on completion.

Latency varies with utterance length and network conditions. Short phrases can feel quick; longer dictation is currently slower.

## Installation

### Prerequisites

- macOS 12 or later
- Rust and Cargo (install from [rustup.rs](https://rustup.rs))
- Python 3
- A Telnyx API key ([sign up here](https://telnyx.com))

### Step by step

**Step 1: Clone and install**

```bash
git clone https://github.com/a692570/bolo.git
cd bolo
./install.sh
```

The install script does the following, in order:
1. Installs PyObjC native macOS helpers if needed
2. Builds the Rust binary
3. Checks for an existing Telnyx API key in `~/.codex/.env` or your environment — if none is found, prompts you for one and saves it to `~/.bolo/env`
4. Registers Bolo as a login item so it starts automatically
5. Starts Bolo

**Step 2: Pick your hotkey**

On first launch, an onboarding dialog appears asking you to pick which key to hold for dictation. Options include Right Option (MacBook built-in), Right Control (external keyboards), F19 (mechanical keyboards), and Caps Lock. Your choice is saved to `~/.bolo/env` and you can change it anytime:

```bash
export BOLO_HOTKEY="right_control"
```

**Step 3: Grant permissions**

Bolo needs two macOS permissions to work. You'll be prompted on first use, or you can grant them manually:

- **Microphone** — Needed to capture audio while you're dictating. Audio is only recorded while your hotkey is held. Nothing is saved to disk.
- **Accessibility** — Needed to paste text into other apps. Without this, Bolo can't insert text.

Grant both in **System Settings > Privacy & Security**. Restart Bolo after granting Accessibility:

```bash
./restart.sh
```

**Step 4: Dictate**

Place your cursor in any text field, hold your hotkey (Tink sound plays), speak, and release (Pop sound plays). Text appears at your cursor and is copied to your clipboard. A native HUD at the bottom of the screen shows Dictating → Thinking → Inserting → Copied. Use the menu bar icon to choose a microphone or quit.

Restart Bolo anytime with:
```bash
./restart.sh
```

## Configuration

Set your Telnyx API key as an environment variable:

```bash
export TELNYX_API_KEY="your_key_here"
```

Add it to your shell profile to persist it, or put it in `~/.bolo/env`. The install script writes prompted keys to `~/.bolo/env`.
Bolo reads `TELNYX_API_KEY` from the process environment first, then falls back to `~/.bolo/env`, `~/.codex/.env`, and `~/.zshrc`.

To change the push-to-talk hotkey, set:

```bash
export BOLO_HOTKEY="right_option"
```

Supported values:

- `right_option` (default): Right Option key
- `right_control`: Right Control key
- `right_shift`: Right Shift key
- `fn`: Fn key
- `f1` through `f19`: Function keys
- `caps_lock`: Caps Lock key

To preselect a microphone without using the menu, set:

```bash
export BOLO_MICROPHONE="Microphone name"
```

To test another Telnyx STT model, set:

```bash
export BOLO_STT_MODEL="deepgram/nova-3"
export BOLO_STT_FALLBACK_MODEL="openai/whisper-large-v3-turbo"
```

For provider fallback, set a comma-separated chain:

```bash
export BOLO_STT_FALLBACKS="xai,assemblyai,telnyx:openai/whisper-large-v3-turbo"
```

Supported fallback entries:

- `telnyx:<model>` uses the existing Telnyx API key.
- `xai` uses `XAI_API_KEY` and xAI's REST STT API.
- `assemblyai` or `assemblyai:<speech_model>` uses `ASSEMBLYAI_API_KEY`. This uploads and polls, so it is best as an emergency fallback rather than the first low-latency backup.

Set `BOLO_STT_FALLBACKS=off` to fail fast instead of retrying when the primary model is rate limited. `BOLO_STT_FALLBACK_MODEL` still works for a single Telnyx fallback model.

To opt into LLM cleanup, set:

```bash
export BOLO_LLM_CLEANUP="on"
```

When LiteLLM is configured, Bolo uses `Kimi-K2.5` for cleanup. Without LiteLLM, it uses Telnyx `Qwen/Qwen3-235B-A22B` with thinking disabled. MiniMax is intentionally not used for cleanup because it can leak reasoning text into the output.

You can also add personal vocabulary in `~/.bolo_vocabulary.json` as a JSON string array, for example:

```json
["Release note", "Remotion", "Telnyx", "Abhishek"]
```

Bolo merges that with its built-in vocabulary and uses it to preserve known terms more reliably.

## Current Limitations

- Bolo is under active development and improving quickly.
- Short dictation works well today. Longer dictation and latency are still improving.
- Long dictation still needs more real-world validation than short phrases.
- Cleanup is intentionally conservative to preserve literal meaning.
- Streaming preview and learned correction memory are not part of the Rust runtime.
- A first-run onboarding dialog asks for your preferred hotkey so you never start with the wrong key.

## Logs

```bash
tail -f /tmp/bolo.log
```

Each dictation logs the pipeline stages: audio metadata, Telnyx STT endpoint/model/request metadata, raw STT transcript, local cleanup transformations, LLM cleanup endpoint/model/input/output when enabled, and the final text sent for insertion. Authorization headers and API keys are not logged.

## Evaluation

To run the small phrase-based evaluation harness:

```bash
cd bolo
python3 eval_dictation.py prompts
python3 eval_dictation.py init > eval_results.json
```

Fill `actual` in `eval_results.json` with what Bolo produced for each phrase, then score it:

```bash
python3 eval_dictation.py score eval_results.json
```

## FAQ

- How does this compare to Wispr Flow?

Bolo is a free open source alternative with similar push-to-talk mechanics. Both use global hotkeys and cloud STT. Bolo uses Telnyx APIs and is fully transparent in audio handling.

- Is my audio stored or used for training?

Audio is sent to Telnyx APIs for transcription and immediately discarded. Bolo processes audio in memory only and does not retain any history.

- Can I change the hotkey from Right Option?

Yes. Set `BOLO_HOTKEY` to `right_control`, `right_shift`, `fn`, `f1` through `f19`, or `caps_lock`. See Configuration above.

- Does it work offline?

No. Bolo requires internet to reach Telnyx APIs.

- Why Rust?

Rust gives Bolo a single compiled runtime with strict linting, safe audio capture, and no app-level unsafe code.

- How do I correct a mistake?

Re-dictate the corrected text. Bolo no longer uses the old learned correction memory because it was too error-prone.

## Troubleshooting

- **No text appears after releasing hotkey**: Check `/tmp/bolo.log`. Verify `TELNYX_API_KEY` is set. Ensure Accessibility permission is granted and Bolo was restarted after granting it.

- **Audio not recording**: Verify Microphone permission is granted in System Settings. Run `./restart.sh` after granting permissions so macOS re-prompts cleanly.

- **Multiple Bolo processes appear**: Run `./restart.sh`.

- **Bolo does not appear in the menu bar**: Check `/tmp/bolo.log` for menu initialization errors and verify you are running the latest `target/release/bolo`.

- **High latency**: Check network connectivity. Longer utterances currently use batch finalization.

## License

MIT

# Bolo, an open source Wispr Flow alternative for macOS

Free, self-hosted voice dictation powered by Telnyx AI. Hold Right Option anywhere to dictate. Release to paste.

Bolo is a macOS push-to-talk app that transcribes your speech and pastes it into any active text field, including Slack, Notion, Gmail, VS Code, terminals, and browsers. No always-on microphone. No subscription.

The name comes from Hindi: "bolo" means "speak."

```bash
git clone https://github.com/a692570/bolo.git
cd bolo
./install.sh
```

## Metadata

- **Category**: Voice dictation, Speech-to-text, Productivity tool
- **Platform**: macOS 12+
- **Language**: Rust 1.88+
- **Dependencies**: cpal, rdev, reqwest, arboard
- **Use-case tags**: voice-to-text, global hotkey, accessibility, speech recognition, STT
- **Related tools**: Wispr Flow alternative, Whisper, macOS Dictation
- **License**: MIT
- **Repository**: https://github.com/a692570/bolo

## How it works

Bolo runs as a menu bar app that monitors global input events and processes audio only while the hotkey is held.

1. **Global hotkey monitoring**: Uses Rust's `rdev` event listener to listen for Option key events system-wide without interfering with other applications.
2. **Audio capture**: On Option press, initializes a `cpal` input stream to capture PCM audio directly to memory.
3. **Recording**: Continues buffering audio while key is held. No disk writes occur during recording.
4. **Key release trigger**: On Right Option release, immediately finalizes audio buffer and initiates API calls.
5. **Speech-to-text**: Sends audio to Telnyx AI API calling `deepgram/nova-3`, with `distil-whisper/distil-large-v2` fallback on rate limits.
6. **Text cleanup**: Applies local transcript cleanup by default. Optional LLM cleanup can be enabled with `BOLO_LLM_CLEANUP=on`.
7. **Text injection**: Uses the system clipboard plus `osascript` Cmd+V automation to paste processed text at the current cursor position.
8. **Audio feedback**: Plays system Tink sound on record start and Pop sound on completion.

Latency varies with utterance length and network conditions. Short phrases can feel quick; longer dictation is currently slower.

## Installation

Requires macOS 12+, Rust/Cargo, and a Telnyx API key.

```bash
git clone https://github.com/a692570/bolo.git
cd bolo
./install.sh
```

The install script builds the Rust binary, migrates an existing key from `~/.codex/.env` if present, prompts for your Telnyx API key if needed, registers the launcher as a Login Item, and starts Bolo.

Restart Bolo later:

```bash
./restart.sh
```

## Permissions

Bolo requires two macOS permissions to function.

**Microphone**: Required to capture audio during dictation. Bolo only accesses the microphone while Right Option is held. No audio is stored locally except any logs you choose to keep.

**Accessibility**: Required to paste text into other applications. Bolo uses system event automation for universal text injection. Without this permission, Bolo cannot insert text into other apps.

Grant both in **System Settings > Privacy & Security**. Restart Bolo after granting Accessibility permission.

## Usage

1. Place cursor in any text field
2. Hold Right Option (Tink sound plays)
3. Speak naturally
4. Release Right Option (Pop sound plays)
5. Transcribed text appears at cursor after finalization

While recording, Bolo shows a small bottom-centered borderless "Listening" overlay. Use the Bolo menu bar item to choose a microphone or quit.

## Configuration

Set your Telnyx API key as an environment variable:

```bash
export TELNYX_API_KEY="your_key_here"
```

Add it to your shell profile to persist it, or put it in `~/.bolo/env`. The install script writes prompted keys to `~/.bolo/env`.
Bolo reads `TELNYX_API_KEY` from the process environment first, then falls back to `~/.bolo/env`, `~/.codex/.env`, and `~/.zshrc`.

To preselect a microphone without using the menu, set:

```bash
export BOLO_MICROPHONE="Microphone name"
```

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
- The legacy Python overlay, streaming preview, and learned correction memory are not part of the Rust runtime.

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

Currently hardcoded. Modify `is_right_option` in `src/main.rs` to change this.

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

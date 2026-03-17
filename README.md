# Bolo, an open source Wispr Flow alternative for macOS

Free, self-hosted voice dictation powered by Telnyx AI. Hold Right Option anywhere to dictate. Release to paste.

Bolo is a macOS menubar app that transcribes your speech and pastes it into any active text field, including Slack, Notion, Gmail, VS Code, terminals, and browsers. No always-on microphone. No subscription.

```bash
git clone https://github.com/a692570/bolo.git
cd bolo
./install.sh
```

## Metadata

- **Category**: Voice dictation, Speech-to-text, Productivity tool
- **Platform**: macOS 12+
- **Language**: Python 3.9+
- **Dependencies**: rumps, sounddevice, pyobjc, requests, websockets
- **Use-case tags**: voice-to-text, global hotkey, menubar app, accessibility, speech recognition, STT
- **Related tools**: Wispr Flow alternative, Whisper, macOS Dictation
- **License**: MIT
- **Repository**: https://github.com/a692570/bolo

## How it works

Bolo runs as a menubar application that monitors global input events and processes audio only while the hotkey is held.

1. **Global hotkey monitoring**: Uses CGEventTap via pyobjc to listen for Right Option key events system-wide without interfering with other applications.
2. **Audio capture**: On Right Option press, initializes sounddevice stream to capture 16kHz mono PCM audio directly to memory buffer.
3. **Recording**: Continues buffering audio while key is held. No disk writes occur during recording.
4. **Key release trigger**: On Right Option release, immediately finalizes audio buffer and initiates API calls.
5. **Speech-to-text**: Sends audio to Telnyx AI API calling `deepgram/nova-3` as primary STT engine (falls back to `distil-whisper/distil-large-v2` on rate limits).
6. **Text cleanup**: Optionally sends raw transcription to Telnyx AI API calling `Qwen/Qwen3-235B-A22B` for minimal punctuation and capitalization cleanup in prose-oriented contexts.
7. **Text injection**: Uses CGEvent keyboard simulation to paste processed text at current cursor position in the active application.
8. **Audio feedback**: Plays system Tink sound on record start and Pop sound on completion.

Latency varies with utterance length and whether Bolo uses streaming preview or safer batch finalization. Short phrases can feel quick; longer dictation is currently slower.

## Concepts

Voice dictation on macOS typically requires either built-in dictation (which requires explicit mode switching and has limited cross-app consistency) or always-on microphone solutions that raise privacy concerns. Bolo implements a push-to-talk model using global hotkeys, ensuring the microphone is only active during explicit user intent. This provides universal text injection across sandboxed and non-sandboxed applications including Slack, Notion, Gmail, code editors, terminals, and browsers.

## Installation

Requires macOS 12+, Python 3.9+, and a Telnyx API key (free tier available at [telnyx.com](https://telnyx.com)).

```bash
git clone https://github.com/a692570/bolo.git
cd bolo
./install.sh
```

The install script installs dependencies, prompts for your Telnyx API key if needed, and registers the existing `start-bolo.command` launcher as a Login Item so Bolo starts automatically on login.

## Permissions

Bolo requires two macOS permissions to function.

**Microphone**: Required to capture audio during dictation. Bolo only accesses the microphone while Right Option is held. No audio is stored locally or transmitted outside Telnyx API calls.

**Accessibility**: Required to paste text into other applications. Bolo uses CGEvent taps to simulate keyboard input for universal text injection. Without this permission, Bolo cannot insert text into other apps.

Grant both in **System Settings > Privacy & Security**. Bolo must be restarted after granting Accessibility for it to take effect.

## Usage

1. Place cursor in any text field
2. Hold Right Option (Tink sound plays)
3. Speak naturally
4. Release Right Option (Pop sound plays)
5. Transcribed text appears at cursor after finalization

Click the menubar icon to see the last transcript or quit.

**Session history**: Click the menubar icon to access your last 10 transcripts. Click any to copy it to clipboard.

## Configuration

Set your Telnyx API key as an environment variable:

```bash
export TELNYX_API_KEY="your_key_here"
```

Add to `~/.zshrc` or `~/.bash_profile` to persist. The install script does this automatically.

You can also add personal vocabulary in `~/.bolo_vocabulary.json` as a JSON string array, for example:

```json
["Release note", "Remotion", "Telnyx", "Abhishek"]
```

Bolo merges that with its built-in vocabulary and uses it to preserve known terms more reliably.

## Current Limitations

- Bolo is under active development and improving quickly.
- Short dictation works well today. Longer dictation and latency are still improving.
- Streaming preview can stall on long speech; Bolo falls back to a listening status and safer finalization.
- Cleanup is intentionally conservative to preserve literal meaning.
- Learned correction memory is currently disabled while a safer replacement is being designed.

## Logs

```bash
tail -f /tmp/bolo.log
```

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

Currently hardcoded. Modify the `_NX_DEVICERALTKEYMASK` logic in the CGEventTap implementation in `bolo.py` to change this.

- Does it work offline?

No. Bolo requires internet to reach Telnyx APIs.

- Why Python instead of Swift?

Python provides rapid iteration for audio processing and API integration. pyobjc gives full access to CoreGraphics for global hotkeys without Objective-C.

- How do I correct a mistake?

Re-dictate the corrected text. Bolo no longer uses the old learned correction memory because it was too error-prone.

## Troubleshooting

- **No text appears after releasing hotkey**: Check `/tmp/bolo.log`. Verify `TELNYX_API_KEY` is set. Ensure Accessibility permission is granted and Bolo was restarted after granting it.

- **Audio not recording**: Verify Microphone permission is granted in System Settings. If Bolo was installed as a Login Item, try restarting it once from `start-bolo.command` so macOS re-prompts permissions cleanly.

- **App appears as Python icon in Dock**: This is fixed in the current version. If you see it, restart Bolo.

- **High latency**: Check network connectivity. Longer utterances currently prefer safer batch finalization, which is slower but more reliable.

## License

MIT

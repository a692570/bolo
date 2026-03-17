# Bolo, an open source Wispr Flow alternative for macOS

Free, self-hosted voice dictation powered by Telnyx AI. Hold Right Option anywhere to dictate. Release to paste.

Bolo is a macOS menubar app that transcribes your speech and pastes it into any active text field, including Slack, Notion, Gmail, VS Code, terminals, and browsers. No always-on microphone. No subscription. ~1.0-1.3 second end-to-end latency.

```bash
git clone https://github.com/a692570/bolo.git
cd bolo
./install.sh
```

## Metadata

- **Category**: Voice dictation, Speech-to-text, Productivity tool
- **Platform**: macOS 12+
- **Language**: Python 3.9+
- **Dependencies**: rumps, sounddevice, pyobjc, Telnyx SDK
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
6. **Text cleanup**: Sends raw transcription to Telnyx AI API calling `Qwen/Qwen3-235B-A22B` with `enable_thinking=false` to add punctuation, fix capitalization, and remove filler words.
7. **Text injection**: Uses CGEvent keyboard simulation to paste processed text at current cursor position in the active application.
8. **Audio feedback**: Plays system Tink sound on record start and Pop sound on completion.

Total latency from key release to text paste averages 1.0-1.3 seconds depending on network conditions and audio length.

## Concepts

Voice dictation on macOS typically requires either built-in dictation (which requires explicit mode switching and has limited cross-app consistency) or always-on microphone solutions that raise privacy concerns. Bolo implements a push-to-talk model using global hotkeys, ensuring the microphone is only active during explicit user intent. This provides universal text injection across sandboxed and non-sandboxed applications including Slack, Notion, Gmail, code editors, terminals, and browsers.

## Installation

Requires macOS 12+, Python 3.9+, and a Telnyx API key (free tier available at [telnyx.com](https://telnyx.com)).

```bash
git clone https://github.com/a692570/bolo.git
cd bolo
./install.sh
```

The install script handles dependency installation, prompts for your Telnyx API key, and registers Bolo as a Login Item so it starts automatically on login.

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
5. Transcribed text appears at cursor within 1-2 seconds

Click the menubar icon to see the last transcript or quit.

**Correction mode**: If you make a mistake, press Right Option again within 3 seconds to re-dictate. Bolo will replace the previous text and learn your correction for next time.

**Session history**: Click the menubar icon to access your last 10 transcripts. Click any to copy it to clipboard.

## Configuration

Set your Telnyx API key as an environment variable:

```bash
export TELNYX_API_KEY="your_key_here"
```

Add to `~/.zshrc` or `~/.bash_profile` to persist. The install script does this automatically.

## Logs

```bash
tail -f /tmp/bolo.log
```

## FAQ

1. How does this compare to Wispr Flow?

Bolo is a free open source alternative with similar push-to-talk mechanics. Both use global hotkeys and cloud STT. Bolo uses Telnyx APIs and is fully transparent in audio handling.

2. Is my audio stored or used for training?

Audio is sent to Telnyx APIs for transcription and immediately discarded. Bolo processes audio in memory only and does not retain any history.

3. Can I change the hotkey from Right Option?

Currently hardcoded. Modify the `_NX_DEVICERALTKEYMASK` logic in the CGEventTap implementation in `bolo.py` to change this.

4. Does it work offline?

No. Bolo requires internet to reach Telnyx APIs.

5. Why Python instead of Swift?

Python provides rapid iteration for audio processing and API integration. pyobjc gives full access to CoreGraphics for global hotkeys without Objective-C.

6. How do I correct a mistake?

Press Right Option again within 3 seconds of the previous transcription. Bolo replaces the old text and learns your correction for future dictations.

## Troubleshooting

**No text appears after releasing hotkey**
Check `/tmp/bolo.log`. Verify `TELNYX_API_KEY` is set. Ensure Accessibility permission is granted and Bolo was restarted after granting it.

**Audio not recording (silent / always transcribes as "you know,")**
This is a Whisper hallucination for silence. Verify Microphone permission is granted in System Settings and that Bolo was launched from a Terminal session (not a LaunchAgent, since background processes cannot access the mic without a GUI session).

**App appears as Python icon in Dock**
This is fixed in the current version. If you see it, restart Bolo.

**High latency**
Check network connectivity. The STT call is the bottleneck at ~600ms. LLM cleanup adds ~400ms TTFT via streaming.

## License

MIT

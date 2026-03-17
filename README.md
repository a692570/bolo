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
- **Dependencies**: rumps, sounddevice, numpy, requests, pyperclip, pyobjc, websockets
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
5. **Speech-to-text**: Sends audio to Telnyx AI APIs for transcription.
6. **Text cleanup**: Sends raw transcription to Telnyx AI chat completions for punctuation and cleanup.
7. **Text injection**: Uses CGEvent keyboard simulation to paste processed text at the current cursor position in the active application.
8. **Audio feedback**: Plays system Tink sound on record start and Pop sound on completion.

Total latency from key release to text paste averages 1.0-1.3 seconds depending on network conditions and audio length.

## Installation

Requires macOS 12+, Python 3.9+, and a Telnyx API key.

```bash
git clone https://github.com/a692570/bolo.git
cd bolo
./install.sh
```

The install script handles dependency installation, prompts for your Telnyx API key, and registers Bolo as a Login Item so it starts automatically on login.

## Permissions

Bolo requires two macOS permissions to function.

**Microphone**: Required to capture audio during dictation. Bolo only accesses the microphone while Right Option is held. No audio is stored locally except any logs you choose to keep.

**Accessibility**: Required to paste text into other applications. Bolo uses CGEvent taps to simulate keyboard input for universal text injection. Without this permission, Bolo cannot insert text into other apps.

Grant both in **System Settings > Privacy & Security**. Restart Bolo after granting Accessibility permission.

## Usage

1. Place the cursor in any text field.
2. Hold Right Option. A Tink sound plays.
3. Speak naturally.
4. Release Right Option. A Pop sound plays.
5. Transcribed text appears at the cursor within 1-2 seconds.

Click the menubar icon to copy the last transcript or quit.

**Correction mode**: If you make a mistake, press Right Option again within 3 seconds to re-dictate. Bolo replaces the previous text and stores the correction locally for future reuse.

## Configuration

Set your Telnyx API key as an environment variable:

```bash
export TELNYX_API_KEY="your_key_here"
```

Add it to `~/.zshrc` or `~/.bash_profile` to persist it. The install script currently appends it to `~/.zshrc`.

## Logs

```bash
tail -f /tmp/bolo.log
```

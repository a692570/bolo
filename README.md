# Bolo

Voice dictation for macOS, powered by [Telnyx](https://telnyx.com) AI.

Hold **Right Option** anywhere → speak → release → text appears in whatever app you're using.

Built as a Wispr Flow alternative using Telnyx's STT and LLM APIs.

## How it works

```
Right Option held  →  Tink sound  →  speak
Right Option released  →  Telnyx STT (Whisper)  →  Telnyx LLM (Qwen3 cleanup)  →  Pop sound  →  text pasted
```

End-to-end latency: ~1.0–1.3 seconds.

## Requirements

- macOS 12+
- Python 3.9+
- A Telnyx API key (free — get one at [telnyx.com](https://telnyx.com))

## Install

```bash
git clone https://github.com/yourusername/bolo
cd bolo
chmod +x install.sh
./install.sh
```

The installer will:
- Install Python dependencies
- Ask for your Telnyx API key and save it to `~/.zshrc`
- Add Bolo as a Login Item (auto-starts on login)
- Launch Bolo immediately

## Permissions

macOS will ask for two permissions on first run:

1. **Microphone** — to capture your voice
2. **Accessibility** — to inject text into other apps via global hotkey

Both are required. Grant them in **System Settings → Privacy & Security**.

## Usage

| Action | Result |
|---|---|
| Hold Right Option | Start recording (Tink sound) |
| Release Right Option | Transcribe and paste (Pop sound) |
| Click menubar icon | See last transcript |

## Configuration

Set your API key as an environment variable:

```bash
export TELNYX_API_KEY="your_key_here"
```

Add this to `~/.zshrc` or `~/.bash_profile` to persist across sessions.

## Logs

```bash
tail -f /tmp/bolo.log
```

## Stack

- **STT**: Telnyx + `distil-whisper/distil-large-v2`
- **LLM**: Telnyx + `Qwen/Qwen3-235B-A22B` (thinking disabled for speed)
- **Hotkey**: CGEventTap via pyobjc
- **Menubar**: rumps
- **Audio**: sounddevice + PortAudio

## License

MIT

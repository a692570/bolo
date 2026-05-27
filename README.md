# Bolo

```
 ██████╗  ██████╗ ██╗      ██████╗
 ██╔══██╗██╔═══██╗██║     ██╔═══██╗
 ██████╔╝██║   ██║██║     ██║   ██║
 ██╔══██╗██║   ██║██║     ██║   ██║
 ██████╔╝╚██████╔╝███████╗╚██████╔╝
 ╚═════╝  ╚═════╝ ╚══════╝ ╚═════╝
```

Free, self-hosted voice dictation powered by Telnyx AI. Hold a key anywhere to dictate. Release to paste.

Bolo is a macOS push-to-talk app that transcribes your speech and pastes it into any active text field, including Slack, Notion, Gmail, VS Code, terminals, and browsers. No always-on microphone. No subscription.

The name comes from Hindi: "bolo" means "speak."

macOS 12+ · Rust 1.88+ · MIT · [Website](https://a692570.github.io/bolo/)

```bash
# Install manually
git clone https://github.com/a692570/bolo.git && cd bolo && ./install.sh
```

> **AI agent prompt:** Copy and paste this into Claude Code, ChatGPT, Cursor, or any coding agent:
>
> *"Install Bolo from github.com/a692570/bolo on my Mac. It needs Rust, Python 3, and a Telnyx API key. Run install.sh, then help me grant Microphone and Accessibility permissions."*

The install script does the following, in order:
1. Installs PyObjC native macOS helpers if needed
2. Builds the Rust binary
3. Checks for an existing Telnyx API key in `~/.codex/.env` or your environment — if none is found, prompts you for one and saves it to `~/.bolo/env`
4. Registers Bolo as a login item so it starts automatically
5. Starts Bolo

**Step 2: Pick your hotkey**

On first launch, an onboarding dialog asks which key you want to hold for dictation. Options include Right Option (MacBook built-in), Right Control (external keyboards), F19 (mechanical keyboards), and Caps Lock. Your choice is saved to `~/.bolo/env`.

```bash
# Change it anytime
export BOLO_HOTKEY="right_control"
```

**Step 3: Grant permissions**

Bolo needs two macOS permissions. Grant both in **System Settings > Privacy & Security**:

- **Microphone** — Captures audio only while your hotkey is held. Nothing saved to disk.
- **Accessibility** — Pastes text into other apps.

```bash
# Restart after granting Accessibility
./restart.sh
```

**Step 4: Dictate**

Place your cursor in any text field, hold your hotkey, speak, and release. Text appears at your cursor. Bolo uses the clipboard for paste insertion, then restores the previous clipboard contents by default. Recent transcripts are available from the Bolo menu bar icon when you need to copy one manually.

```bash
# Restart anytime
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

To keep dictated text on the clipboard after insertion, set:

```bash
export BOLO_PRESERVE_CLIPBOARD="off"
```

By default, Bolo restores whatever was on your clipboard before dictation. The dictated text is still kept in Bolo's correction state for commands like `scratch that` and `actually ...`.

Bolo also stores your 10 most recent transcripts locally at `~/.bolo/transcripts.json`. Use the menu bar icon to copy the latest transcript or a recent transcript into the clipboard on demand.

To opt into LLM cleanup, set:

```bash
export BOLO_LLM_CLEANUP="on"
```

When LiteLLM is configured, Bolo uses `Kimi-K2.5` for cleanup. Without LiteLLM, it uses Telnyx `Qwen/Qwen3-235B-A22B` with thinking disabled. MiniMax is intentionally not used for cleanup because it can leak reasoning text into the output.

When LLM cleanup is enabled, Bolo also reads the frontmost app and nearby cursor text through macOS Accessibility so cleanup can choose natural spacing, capitalization, and continuation. That context is used only for cleanup prompting.

You can also add personal vocabulary in `~/.bolo_vocabulary.json` as a JSON string array, for example:

```json
["Release note", "Remotion", "Telnyx", "Abhishek"]
```

Bolo merges that with its built-in vocabulary and uses it to preserve known terms more reliably.

For deterministic phrase fixes after transcription, add replacements in `~/.bolo/replacements.json`:

```json
{
  "voice ai": "Voice AI",
  "opsen sourced": "open sourced"
}
```

Replacements are applied after local cleanup and before text insertion. Longer replacement triggers win before shorter ones.

## Current Limitations

- Bolo is under active development and improving quickly.
- Short dictation works well today. Longer dictation and latency are still improving.
- Long dictation still needs more real-world validation than short phrases.
- Cleanup is intentionally conservative to preserve literal meaning.
- Streaming preview and learned correction memory are not part of the Rust runtime.
- A first-run onboarding dialog asks for your preferred hotkey so you never start with the wrong key.
- Bolo rechecks OS key state while running so missed hotkey press or release events are corrected quickly.
- A recording lifecycle state machine ignores duplicate press and release events and keeps stale-recording recovery predictable.

## Logs

```bash
tail -f /tmp/bolo.log
```

Each dictation logs the pipeline stages: audio metadata, speech detection metrics, Telnyx STT endpoint/model/request metadata, raw STT transcript, local cleanup transformations, LLM cleanup endpoint/model/input/output when enabled, and the final text sent for insertion. Authorization headers, API keys, and clipboard contents are not logged.

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

Audio is sent to Telnyx APIs for transcription and immediately discarded. Bolo processes audio in memory only. Text transcripts are stored locally at `~/.bolo/transcripts.json` for menu bar history.

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

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
1. Creates a private Python helper environment at `~/.bolo/venv`
2. Builds the Rust binary
3. Checks for an existing Telnyx API key in `~/.codex/.env` or your environment. If none is found, it prompts you and saves the key to `~/.bolo/env`
4. Registers Bolo as a login item so it starts automatically
5. Starts Bolo

**Step 2: Pick your hotkey**

On first launch, an onboarding dialog asks which key you want to hold for dictation. Options include Right Option (MacBook built-in), Right Control (external keyboards), F19 (mechanical keyboards), and Caps Lock. Your choice is saved to `~/.bolo/env`.

```bash
# Change it anytime
export BOLO_HOTKEY="right_control"
```

**Step 3: Grant permissions**

Bolo needs two macOS permissions in **System Settings > Privacy & Security**:

- **Microphone**: Captures audio only while your hotkey is held. Nothing is saved to disk.
- **Accessibility**: Bolo's Python helper pastes text into other apps. Add this managed interpreter:

```bash
echo "$HOME/.bolo/venv/bin/python3"

# Restart after enabling that interpreter in Accessibility
./restart.sh
```

**Step 4: Dictate**

Place your cursor in any text field, hold your hotkey, speak, and release. Text appears at your cursor. Bolo uses the clipboard for paste insertion, then restores the previous clipboard contents by default. Recent transcripts are available from the Bolo menu bar icon when you need to copy one manually.

```bash
# Restart anytime
./restart.sh
```

## Updates

Bolo checks for updates when it starts from the login item or `./restart.sh`.
If the local checkout can fast-forward to GitHub, Bolo pulls the latest code and rebuilds the release binary before launching.

You can also use **Check for Updates** from the Bolo menu bar icon.

Updates are skipped if local files have uncommitted changes, GitHub is unreachable, Rust is missing, or the checkout cannot fast-forward cleanly.

To disable launch-time update checks persistently, add this line to `~/.bolo/env`:

```bash
BOLO_AUTO_UPDATE=off
```

## Configuration

Set your Telnyx API key as an environment variable:

```bash
export TELNYX_API_KEY="your_key_here"
```

Add it to your shell profile to persist it, or put it in `~/.bolo/env`. The install script writes prompted keys to `~/.bolo/env`.
Bolo reads `TELNYX_API_KEY` from the process environment first, then falls back to `~/.bolo/env`, `~/.codex/.env`, and `~/.zshrc`.
The installer keeps `~/.bolo` private to your macOS account and stores `~/.bolo/env` with owner-only permissions.

Bolo redacts dictated text from `/tmp/bolo.log` by default. To temporarily log transcript text while debugging, set:

```bash
export BOLO_LOG_TRANSCRIPTS="on"
```

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

To paste the latest transcript again with a separate hotkey, set:

```bash
export BOLO_PASTE_LAST_HOTKEY="f19"
```

This is off unless configured. It accepts the same key names as `BOLO_HOTKEY` where supported, and uses the clipboard only long enough to paste before restoring the previous clipboard text.

A watchdog auto-releases a recording that runs longer than 30 seconds, which protects against a stuck recording but also caps a single dictation. To raise (or lower) that ceiling, set:

```bash
export BOLO_MAX_RECORDING_SECONDS="180"
```

The value is in seconds and defaults to 30. A missing, zero, or unparseable value keeps the 30-second default.

To preselect a microphone without using the menu, set:

```bash
export BOLO_MICROPHONE="Microphone name"
```

To test another Telnyx STT model, set:

```bash
export BOLO_STT_MODEL="deepgram/nova-3"
export BOLO_STT_FALLBACK_MODEL="openai/whisper-large-v3-turbo"
```

For accent or language hints, set:

```bash
export BOLO_STT_LANGUAGE="en-IN"
```

Bolo also exposes this from the menu bar under **Language**. `auto` maps to Deepgram multi-language mode and disables the hint for Whisper-style fallback models.

If a non-US language hint makes recognition slow, Bolo automatically switches back to English, US after that dictation and shows a notification. This keeps accent experiments from making the app feel stuck.

For provider fallback, set a comma-separated chain:

```bash
export BOLO_STT_FALLBACKS="xai,assemblyai,telnyx:openai/whisper-large-v3-turbo"
```

Supported fallback entries:

- `telnyx:<model>` uses the existing Telnyx API key.
- `xai` uses `XAI_API_KEY` and xAI's REST STT API.
- `assemblyai` or `assemblyai:<speech_model>` uses `ASSEMBLYAI_API_KEY`. This uploads and polls, so it is best as an emergency fallback rather than the first low-latency backup.

Set `BOLO_STT_FALLBACKS=off` to fail fast instead of retrying when the primary model is rate limited. `BOLO_STT_FALLBACK_MODEL` still works for a single Telnyx fallback model.

Deepgram streaming STT is enabled by default with the default `deepgram/nova-3` model. It starts sending audio while you speak and falls back to batch transcription when a complete result is not ready quickly enough after release.

To disable streaming and always use batch transcription, set:

```bash
export BOLO_STT_STREAMING="off"
```

Streaming uses the existing Telnyx API key. Supported values are `deepgram` for Nova-3 streaming and `assemblyai` for AssemblyAI Universal-Streaming through Telnyx. Selecting a different `BOLO_STT_MODEL` keeps streaming off unless `BOLO_STT_STREAMING` is explicitly set.

To keep dictated text on the clipboard after insertion, set:

```bash
export BOLO_PRESERVE_CLIPBOARD="off"
```

By default, Bolo restores whatever was on your clipboard before dictation, including non-text pasteboard contents when macOS allows it. The dictated text is still kept in Bolo's correction state for commands like `scratch that` and `actually ...`.

Follow-up edits are guarded by the current cursor position. `scratch that` and `actually ...` only change text when the previous dictation is still immediately before the caret and still matches exactly. If the cursor moved or the text changed, Bolo leaves the document alone.

You can also transform the previous dictation by voice:

```text
Bolo, polish that
Bolo, prompt that
```

`polish that` tightens grammar and flow without changing meaning or confidence. `prompt that` restructures the dictation into a concise goal and supporting requirements. Both commands use the configured Telnyx or LiteLLM rewrite path, and both refuse to act if the previous dictation moved or changed.

Bolo also stores your 10 most recent transcripts locally at `~/.bolo/transcripts.json`. Use the menu bar icon to copy the latest transcript or a recent transcript into the clipboard on demand. When cleanup changes the text, Bolo keeps both the raw STT transcript and the cleaned transcript so you can copy either one from history. If you immediately edit inserted text with Backspace or Cmd+A, Bolo marks only that history item as edited and logs a content-free quality signal.

Use **Clear Transcript History** from the menu bar to delete local transcript history, or **Run Health Check** to confirm Bolo can see microphones, the active language, STT mode, and history state.

To rewrite text already in another app, select the text, open the Bolo menu bar icon, and choose **Rewrite Selected Text...**. Bolo captures the selected text through macOS Accessibility, asks what change you want, runs the same LLM backend, then pastes the replacement back into the original app.

LLM cleanup defaults to `auto`: short clean dictations skip the LLM, while longer or messy dictations use the LLM for grammar, punctuation, contractions, and missing articles.

To force LLM cleanup for every dictation, set:

```bash
export BOLO_LLM_CLEANUP="on"
```

To disable LLM cleanup entirely, set `BOLO_LLM_CLEANUP="off"`.

When LiteLLM is configured, Bolo uses `Kimi-K2.5` for cleanup unless `BOLO_LLM_MODEL` is set. Without LiteLLM, it uses Telnyx `Qwen/Qwen3-235B-A22B` with thinking disabled. MiniMax is intentionally not used for cleanup because it can leak reasoning text into the output.

When LLM cleanup runs, Bolo also reads the frontmost app and nearby cursor text through macOS Accessibility so cleanup can choose natural spacing, capitalization, and continuation. That context is used only for cleanup prompting.

Cleanup style is selected automatically for common email, chat, and notes apps. To override an app, use **Set Cleanup Style for Current App...** from the menu bar and enter `default`, `email`, `chat`, or `notes`.

Overrides are saved in `~/.bolo/prompt_bindings.json`:

```json
[
  {
    "bundle_id": "com.tinyspeck.slackmacgap",
    "app_name": "Slack",
    "profile": "chat"
  }
]
```

You can also add personal vocabulary in `~/.bolo_vocabulary.json` as a JSON string array, for example:

```json
["Release note", "Remotion", "Telnyx", "Abhishek"]
```

You can also teach Bolo common mishears with alias objects:

```json
[
  "Telnyx",
  {
    "text": "Claude",
    "aliases": ["cloud", "claud"]
  },
  {
    "text": "cron",
    "aliases": ["chrome"]
  }
]
```

Bolo merges that with its built-in vocabulary and uses it to preserve known terms more reliably. Aliases are applied locally before LLM cleanup, so common accent or model mistakes are fixed without adding latency.

You can add vocabulary from the menu bar with **Add Vocabulary Term...** and add mishear aliases with **Add Vocabulary Alias...**. New terms are saved to `~/.bolo_vocabulary.json` and start applying immediately.

For deterministic phrase fixes after transcription, add replacements in `~/.bolo/replacements.json`:

```json
{
  "voice ai": "Voice AI",
  "opsen sourced": "open sourced"
}
```

Replacements are applied after local cleanup and before text insertion. Longer replacement triggers win before shorter ones.

You can add these from the menu bar with **Add Correction Rule...**. Rules are saved to `~/.bolo/replacements.json` and apply to the next dictation.

You can also add a correction by voice:

```text
Correct tab only to Tavily
```

Bolo saves that as a local replacement rule and does not paste the command text.

## Current Limitations

- Bolo is under active development and improving quickly.
- Short dictation works well today. Longer dictation and latency are still improving.
- Long dictation still needs more real-world validation than short phrases.
- Cleanup is intentionally conservative to preserve literal meaning.
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

Say `Correct heard phrase to desired phrase`, or use **Add Correction Rule...** from the menu bar. Bolo stores the rule locally in `~/.bolo/replacements.json`.

## Troubleshooting

- **No text appears after releasing hotkey**: Check `/tmp/bolo.log`. Verify `TELNYX_API_KEY` is set. Ensure `~/.bolo/venv/bin/python3` is enabled in Accessibility, then run `./restart.sh`.

- **Paste stops working after an update**: Run `./install.sh` to verify the managed helper, enable `~/.bolo/venv/bin/python3` in System Settings > Privacy & Security > Accessibility, then run `./restart.sh`. Bolo reports helper failures separately from missing permissions.

- **Audio not recording**: Verify Microphone permission is granted in System Settings. Run `./restart.sh` after granting permissions so macOS re-prompts cleanly.

- **Multiple Bolo processes appear**: Run `./restart.sh`.

- **Bolo does not appear in the menu bar**: Check `/tmp/bolo.log` for menu initialization errors and verify you are running the latest `target/release/bolo`.

- **High latency**: Check network connectivity. Longer utterances currently use batch finalization.

## License

MIT

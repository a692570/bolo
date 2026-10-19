//! The qwen rung: a managed Python sidecar (qwen_sidecar.py) that keeps
//! `mlx-community/Qwen3-ASR-0.6B-bf16` resident in a child process via
//! qwen3-asr-mlx on Apple Silicon Metal. It is the offline ladder's
//! accuracy-and-multilingual rung: where parakeet is English-only (a
//! Devanagari clip comes back romanized), qwen measured 52.0% WER on
//! conversational Hindi with native Devanagari output and English
//! content accuracy on par with whisper-large-v3-turbo, at 0.166 s warm
//! per clip and ~0.34 s load from the warmed HF cache (benchmark
//! 2026-10-18, ~/Projects/bolo-model-bench/RESULTS.md).
//!
//! Lifecycle mirrors the parakeet rung exactly (same shape as the
//! accessibility daemon): the runtime spawns the child lazily (first
//! local transcription), confirms the `{"type":"ready"}` handshake
//! before any request goes out, and answers one
//! `{"type":"transcribe","id":N,"wav_path":...}` request (plus optional
//! `language` and `context` fields) with one `{"type":"result",...}`
//! line. A sidecar that dies or stalls past the per-request budget is
//! dropped from its slot; the next request spawns a fresh child, so
//! recovery costs at most one attempt. The weights download on first
//! use through huggingface_hub inside the sidecar (the same HF cache
//! parakeet rides), so `rung_available` checks the interpreter and
//! script only, matching parakeet.
//!
//! The interpreter resolves through `BOLO_QWEN_PYTHON`, then the managed
//! `~/.bolo/qwen-venv/bin/python3` (created by
//! `ensure-python-env.sh --qwen`, separate from the helper venv because
//! qwen3-asr-mlx 0.3.0 needs >= 3.10). With neither present the rung
//! reports itself unavailable and the ladder falls to the whisper rung
//! unchanged. The model repo id resolves through `BOLO_QWEN_MODEL`
//! (default the benchmark model) and travels to the child as the
//! spawn-time `BOLO_QWEN_MODEL` environment variable.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use tracing::{info, warn};

use crate::AppError;

/// The model the 2026-10-18 benchmark measured: native Devanagari Hindi
/// (52.0% WER, best of the field) with English content accuracy on par
/// with whisper-large-v3-turbo, ~1.2 GB bf16, Apache-2.0.
pub(crate) const QWEN_MODEL_ID: &str = "mlx-community/Qwen3-ASR-0.6B-bf16";
/// The sidecar script, resolved against the app's root dir (repo root
/// in a source run, Resources in the bundle): the same convention the
/// parakeet rung uses.
pub(crate) const QWEN_SIDECAR_SCRIPT: &str = "qwen_sidecar.py";
/// One transcription round-trip. The benchmark measured 0.166 s warm per
/// clip; 10 s is the same generous-but-bounded budget the parakeet rung
/// uses, so a wedged inference still falls to the next rung in bounded
/// time.
pub(crate) const QWEN_REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
/// Bounded startup: the venv python must import MLX, resolve the HF
/// snapshot (warm cache ~0.34 s, cold cache a one-time ~1.2 GB
/// download) and emit `ready`. The same 30 s ceiling the parakeet rung
/// uses bounds a first-ever download-while-loading too; a load that
/// outruns it is abandoned and retried on the next dictation.
pub(crate) const QWEN_STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
/// Managed venv created by `ensure-python-env.sh --qwen`; a constant
/// apart from the helper venv because qwen3-asr-mlx needs Python >= 3.10.
const QWEN_VENV_PYTHON: &str = ".bolo/qwen-venv/bin/python3";
/// Clips at least this long lead the offline ladder with qwen (the
/// accuracy rung) instead of parakeet (the speed rung). The value reuses
/// the codebase's existing long-clip notion: `streaming_batch_fallback_reason`
/// already treats 6 s as the boundary after which a clip is judged as a
/// long dictation. Engaging qwen costs one boolean reorder of the
/// ladder (bench: 0.166 s warm per clip), never an extra transcription.
pub(crate) const QWEN_LEADS_CLIP_THRESHOLD: Duration = Duration::from_secs(6);

/// Parse `BOLO_LOCAL_QWEN`: the qwen rung is on unless explicitly
/// disabled. Recognizes the same off vocabulary as
/// [`crate::load_bool_env`] around a default of enabled, mirroring
/// `parse_parakeet_enabled`: `off`/`false`/`0`/`no` disable the rung,
/// everything else (including the absent value) keeps it active, so
/// the default matches `BOLO_LOCAL_STT=auto` behavior.
pub(crate) fn parse_qwen_enabled(value: Option<&str>) -> bool {
    match value
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase()
        .as_str()
    {
        "off" | "false" | "0" | "no" => false,
        _ => true,
    }
}

/// Whether the qwen rung may serve a language hint. qwen3-asr covers 30
/// languages; Bolo only pins two of them in practice, so the rung is
/// conservative: eligible for every `en`/`hi` hint and for `auto`
/// (qwen auto-detects; an empty hint means detection, the same
/// convention `local_whisper::whisper_language` applies). Any other
/// explicit code names a language this build has not measured, so the
/// rung declines it and whisper detection handles the clip.
pub(crate) fn qwen_language_eligible(configured: &str) -> bool {
    let base = configured.trim().to_ascii_lowercase();
    if base.is_empty() || base == "auto" {
        return true;
    }
    matches!(base.split(['-', '_']).next(), Some("en" | "hi"))
}

/// Whether one clip leads the offline ladder with qwen ahead of
/// parakeet. Short clips keep parakeet first (0.116 s warm per clip,
/// also the rung feeding the live offline preview); clips at the
/// threshold or longer lead with qwen's accuracy
/// (`QWEN_LEADS_CLIP_THRESHOLD`). Pure so the routing decision is
/// pinned by tests.
pub(crate) fn qwen_runs_first(recording_duration: Duration) -> bool {
    recording_duration >= QWEN_LEADS_CLIP_THRESHOLD
}

/// The qwen rung's context prompt from Bolo's vocabulary, mirroring the
/// cloud rung's prompt terms (the first 50): qwen3-asr's `transcribe`
/// accepts a free-form `context` string its own docs show as a
/// vocabulary hint (`"Vocabulary: ..."`). `None` when the vocabulary
/// carries nothing, so the request stays minimal.
pub(crate) fn vocabulary_context(terms: &[String]) -> Option<String> {
    if terms.is_empty() {
        return None;
    }
    let joined = terms
        .iter()
        .take(50)
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(", ");
    Some(format!("Vocabulary: {joined}."))
}

/// The interpreter the sidecar runs under: `BOLO_QWEN_PYTHON` pinned by
/// the user, else the managed qwen venv. `None` disables the rung (the
/// venv is a deliberate, one-time `--qwen` install).
pub(crate) fn qwen_python_executable() -> Option<PathBuf> {
    if let Some(value) = crate::load_env_value("BOLO_QWEN_PYTHON") {
        let path = PathBuf::from(value);
        if path.is_file() {
            return Some(path);
        }
        warn!(
            "[stt] qwen_python_unusable {}",
            serde_json::json!({"path": path.display().to_string()})
        );
        return None;
    }
    let managed = crate::home_path(QWEN_VENV_PYTHON);
    if managed.is_file() {
        return Some(managed);
    }
    None
}

/// Path of the sidecar script inside the app's root dir.
pub(crate) fn qwen_script_path(root_dir: &Path) -> PathBuf {
    root_dir.join(QWEN_SIDECAR_SCRIPT)
}

/// A fresh temp WAV path for one qwen attempt: names collide never (pid
/// plus nanoseconds), land in the system temp dir the tests use, and
/// the attempt's worker removes the file after the exchange.
pub(crate) fn temp_recording_path() -> Option<PathBuf> {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_nanos();
    Some(std::env::temp_dir().join(format!("bolo-qwen-{}-{nanos}.wav", std::process::id())))
}

/// The local HF cache directory holding weights snapshots:
/// `HF_HUB_CACHE`, else `HF_HOME/hub`, else the default
/// `~/.cache/huggingface/hub`. The same resolution order
/// huggingface_hub itself applies.
fn hf_hub_cache_dir() -> PathBuf {
    if let Some(value) = crate::load_env_value("HF_HUB_CACHE") {
        let path = PathBuf::from(value);
        if path.is_dir() {
            return path;
        }
    }
    if let Some(value) = crate::load_env_value("HF_HOME") {
        let path = PathBuf::from(value).join("hub");
        if path.is_dir() {
            return path;
        }
    }
    crate::home_path(".cache/huggingface/hub")
}

/// Whether the qwen rung's weights resolve from the local HF cache: a
/// snapshot revision holding both `config.json` and a non-trivial
/// `model.safetensors`. Presence drives the tray menu's qwen item; the
/// rung itself does not gate on it, because the sidecar downloads on
/// first use and a missing snapshot surfaces as a stalled handshake
/// that the startup budget already bounds.
pub(crate) fn model_present(model_id: &str) -> bool {
    model_present_in(&hf_hub_cache_dir(), model_id)
}

/// [`model_present`] against an explicit cache root, so the directory
/// mapping tests without touching the environment: `repo/name` becomes
/// `models--repo--name`, and a snapshot revision counts only when its
/// config and non-trivial weights are both on disk.
fn model_present_in(cache_dir: &Path, model_id: &str) -> bool {
    let trimmed = model_id.trim();
    if trimmed.is_empty() {
        return false;
    }
    let repo_dir = cache_dir.join(format!("models--{}", trimmed.replace('/', "--")));
    let snapshots = repo_dir.join("snapshots");
    let Ok(revisions) = std::fs::read_dir(&snapshots) else {
        return false;
    };
    for revision in revisions.flatten() {
        if !revision.path().is_dir() {
            continue;
        }
        let config = revision.path().join("config.json");
        let weights = revision.path().join("model.safetensors");
        let Ok(weights_meta) = std::fs::metadata(&weights) else {
            continue;
        };
        if config.is_file() && weights_meta.len() > 1_048_576 {
            return true;
        }
    }
    false
}

/// Why one sidecar round-trip failed; drives the ladder-fallback log.
#[derive(Debug)]
enum SidecarFailure {
    Timeout,
    Exited,
    Io(std::io::Error),
    Sidecar(String),
}

impl SidecarFailure {
    fn reason(&self) -> String {
        match self {
            Self::Timeout => String::from("sidecar_timeout"),
            Self::Exited => String::from("sidecar_exit"),
            Self::Io(error) => format!("sidecar_io ({error})"),
            Self::Sidecar(message) => format!("sidecar_error ({message})"),
        }
    }
}

/// A live sidecar child plus the pipes to talk to it. Exactly the
/// parakeet rung's shape: one reader thread owns stdout and the request
/// path reads parsed replies through a bounded channel.
struct QwenSidecar {
    child: Child,
    stdin: ChildStdin,
    replies: Receiver<String>,
}

impl Drop for QwenSidecar {
    fn drop(&mut self) {
        // Kill first, then reap: no zombie, and the reader thread sees EOF
        // even if the child was wedged mid-request.
        let _kill_result = self.child.kill();
        let _wait_result = self.child.wait();
    }
}

impl QwenSidecar {
    /// Spawn the sidecar under `python` without touching the process-wide
    /// slot, so a slow warm-up can never hold the slot's lock. The model
    /// id travels as the spawn-time `BOLO_QWEN_MODEL` the child reads.
    fn start(python: &Path, script: &Path, model_id: &str) -> Result<Self, String> {
        if !script.exists() {
            return Err(format!("{} is missing", script.display()));
        }
        let mut child = Command::new(python)
            .arg(script)
            .env("BOLO_PARENT_PID", std::process::id().to_string())
            .env("BOLO_QWEN_MODEL", model_id)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|error| format!("spawn failed: {error}"))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| String::from("sidecar stdin unavailable"))?;
        let stdout: ChildStdout = child
            .stdout
            .take()
            .ok_or_else(|| String::from("sidecar stdout unavailable"))?;
        let (sender, replies) = std::sync::mpsc::channel();
        let reader = std::thread::Builder::new()
            .name(String::from("bolo-qwen-reader"))
            .spawn(move || {
                let reader = BufReader::new(stdout);
                for line in reader.lines() {
                    match line {
                        Ok(text) => {
                            // Stop draining the moment nobody reads: the
                            // waiter is gone or the slot was retired.
                            if sender.send(text).is_err() {
                                break;
                            }
                        }
                        Err(_) => break,
                    }
                }
            })
            .map_err(|error| format!("reader thread failed: {error}"))?;
        drop(reader);
        Ok(Self {
            child,
            stdin,
            replies,
        })
    }

    /// Whether the child already terminated.
    fn has_exited(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(Some(_)) | Err(_))
    }

    /// Wait for the `{"type":"ready"}` handshake, bounded by the startup
    /// constant. Built on the deadline form so tests bound it tightly.
    fn wait_ready(&mut self) -> Result<(), SidecarFailure> {
        self.wait_ready_bounded(QWEN_STARTUP_TIMEOUT)
    }

    /// [`QwenSidecar::wait_ready`] with an explicit budget.
    fn wait_ready_bounded(&mut self, budget: Duration) -> Result<(), SidecarFailure> {
        let deadline = Instant::now() + budget;
        loop {
            let spare = deadline.saturating_duration_since(Instant::now());
            if spare.is_zero() {
                return Err(SidecarFailure::Timeout);
            }
            let line = wait_for_sidecar_reply(&self.replies, spare)?;
            if parse_ready_line(&line) {
                return Ok(());
            }
            // Anything else before ready is out-of-protocol chatter;
            // keep draining until the handshake or the budget runs out.
            warn!(
                "[stt] qwen_unexpected_prewrite_line {}",
                serde_json::json!({"line": line})
            );
        }
    }

    /// Send one transcribe request and return the sidecar's text for
    /// exactly this `id`, parsed by [`parse_transcribe_reply`], bounded
    /// by the request constant. `language` is Bolo's raw resolved hint
    /// (the sidecar maps it onto qwen's language names); `context` is
    /// the optional vocabulary string. `None` keeps the field off the
    /// wire.
    fn transcribe(
        &mut self,
        id: u64,
        wav_path: &Path,
        language: Option<&str>,
        context: Option<&str>,
    ) -> Result<String, SidecarFailure> {
        self.transcribe_bounded(id, wav_path, language, context, QWEN_REQUEST_TIMEOUT)
    }

    /// [`QwenSidecar::transcribe`] with an explicit budget.
    fn transcribe_bounded(
        &mut self,
        id: u64,
        wav_path: &Path,
        language: Option<&str>,
        context: Option<&str>,
        budget: Duration,
    ) -> Result<String, SidecarFailure> {
        let mut request = serde_json::json!({
            "type": "transcribe",
            "id": id,
            "wav_path": wav_path.display().to_string(),
        });
        if let Some(language) = language {
            request["language"] = serde_json::json!(language);
        }
        if let Some(context) = context {
            request["context"] = serde_json::json!(context);
        }
        let line = request.to_string();
        if let Err(error) = self
            .stdin
            .write_all(line.as_bytes())
            .and_then(|()| self.stdin.write_all(b"\n"))
            .and_then(|()| self.stdin.flush())
        {
            return Err(SidecarFailure::Io(error));
        }
        // One deadline spans every reply line, so a chatty child cannot
        // reset the clock gossiping: non-matching lines drain the
        // remaining budget, never extend it.
        let deadline = Instant::now() + budget;
        loop {
            let spare = deadline.saturating_duration_since(Instant::now());
            if spare.is_zero() {
                return Err(SidecarFailure::Timeout);
            }
            let reply = wait_for_sidecar_reply(&self.replies, spare)?;
            match parse_transcribe_reply(&reply, id) {
                Some(Ok(text)) => return Ok(text),
                Some(Err(message)) => return Err(SidecarFailure::Sidecar(message)),
                None => {
                    warn!(
                        "[stt] qwen_unexpected_reply_line {}",
                        serde_json::json!({"wanted_id": id, "line": reply})
                    );
                }
            }
        }
    }
}

/// Wait for the sidecar's next response line. Split from the sidecar
/// struct so the timeout path tests without a process.
fn wait_for_sidecar_reply(
    replies: &Receiver<String>,
    timeout: Duration,
) -> Result<String, SidecarFailure> {
    match replies.recv_timeout(timeout) {
        Ok(line) => Ok(line),
        Err(RecvTimeoutError::Timeout) => Err(SidecarFailure::Timeout),
        Err(RecvTimeoutError::Disconnected) => Err(SidecarFailure::Exited),
    }
}

/// `true` when the line is the ready handshake.
fn parse_ready_line(line: &str) -> bool {
    match serde_json::from_str::<serde_json::Value>(line) {
        Ok(reply) => reply.get("type").and_then(serde_json::Value::as_str) == Some("ready"),
        Err(_) => false,
    }
}

/// Parse one reply to a transcribe request. `Some(Ok(text))` for a result
/// matching `id` (empty text included: the energy-gated no-speech
/// answer), `Some(Err(message))` for the sidecar's own error reply,
/// `None` when the line is malformed, an id mismatch, or gossip. The
/// optional `language` field the qwen sidecar reports is accepted and
/// ignored here; the ladder consumes only the text.
fn parse_transcribe_reply(line: &str, id: u64) -> Option<Result<String, String>> {
    let reply: serde_json::Value = serde_json::from_str(line).ok()?;
    match reply.get("type").and_then(serde_json::Value::as_str) {
        Some("result") => {
            let reply_id = reply.get("id").and_then(serde_json::Value::as_u64)?;
            if reply_id != id {
                return None;
            }
            let text = reply
                .get("text")
                .and_then(serde_json::Value::as_str)?
                .to_owned();
            Some(Ok(text))
        }
        Some("error") => {
            let message = reply
                .get("message")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
                .unwrap_or_else(|| String::from("unspecified sidecar error"));
            Some(Err(message))
        }
        _ => None,
    }
}

/// Monotonic request ids, so a stale reply from a mid-request restart can
/// never be mistaken for the answer to the next attempt.
static REQUEST_ID: AtomicU64 = AtomicU64::new(1);
/// The process-wide sidecar slot. First local transcription spawns the
/// child; the slot outlives App instances exactly like the parakeet
/// rung's slot.
static QWEN_SIDECAR: OnceLock<Mutex<Option<QwenSidecar>>> = OnceLock::new();

fn qwen_sidecar_cell() -> &'static Mutex<Option<QwenSidecar>> {
    QWEN_SIDECAR.get_or_init(|| Mutex::new(None))
}

/// Spawn a sidecar and confirm the ready handshake before it goes anywhere
/// near the slot, so callers never wait on interpreter startup twice.
fn spawn_ready_sidecar(
    python: &Path,
    script: &Path,
    model_id: &str,
) -> Result<QwenSidecar, String> {
    let mut sidecar = QwenSidecar::start(python, script, model_id)?;
    sidecar
        .wait_ready()
        .map_err(|failure| format!("ready handshake failed: {}", failure.reason()))?;
    match sidecar.child.try_wait() {
        Ok(None) => Ok(sidecar),
        Ok(Some(status)) => Err(format!(
            "sidecar exited during startup (code {} signal {})",
            status.code().unwrap_or(-1),
            status.signal().unwrap_or(0)
        )),
        Err(error) => Err(format!("sidecar status unavailable: {error}")),
    }
}

/// Whether the slot holds a live sidecar.
fn sidecar_is_running() -> bool {
    let mut cell = qwen_sidecar_cell()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    match cell.as_mut() {
        Some(sidecar) => !sidecar.has_exited(),
        None => false,
    }
}

/// Store a freshly warmed sidecar when the slot is free; the loser of a
/// race is killed by its own Drop, keeping the single-sidecar invariant.
fn store_sidecar(sidecar: QwenSidecar) -> bool {
    let mut cell = qwen_sidecar_cell()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if cell.is_some() {
        return false;
    }
    *cell = Some(sidecar);
    true
}

/// Drop the slot's sidecar (kill on Drop), so the next request starts a
/// fresh child: the lazy restart loop. Used by the request path on
/// failure and by tests for teardown.
fn retire_sidecar(reason: &str) {
    let mut cell = qwen_sidecar_cell()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if cell.take().is_some() {
        info!("[stt] qwen_sidecar_retired {}", reason);
    }
}

/// One transcription through a sidecar the caller resolved: the slot
/// machine plus the exchange. `Err` (missing script, wedged child,
/// timeout) leaves the ladder to fall through to the next rung.
/// Budgets use the startup and request constants; the request id is
/// monotonic.
fn transcribe_with(
    python: &Path,
    script: &Path,
    model_id: &str,
    wav_path: &Path,
    language: Option<&str>,
    context: Option<&str>,
) -> Result<String, AppError> {
    if !script.exists() {
        return Err(AppError::Transcription(format!(
            "qwen sidecar script is missing: {}",
            script.display()
        )));
    }
    // Ensure a live child: prune the exited one, spawn and handshake
    // outside the slot lock so nothing else ever waits on startup.
    if !sidecar_is_running() {
        retire_sidecar("previous child exited");
        match spawn_ready_sidecar(python, script, model_id) {
            Ok(sidecar) => {
                if !store_sidecar(sidecar) {
                    info!(
                        "[stt] qwen_sidecar_start_raced_with_another; keeping the existing child"
                    );
                }
            }
            Err(reason) => {
                warn!("[stt] qwen_sidecar_start_failed {}", reason);
                return Err(AppError::Transcription(reason));
            }
        }
    }
    let id = REQUEST_ID.fetch_add(1, Ordering::SeqCst);
    let started = Instant::now();
    let mut cell = qwen_sidecar_cell()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some(sidecar) = cell.as_mut() else {
        return Err(AppError::Transcription(String::from(
            "qwen sidecar slot is empty",
        )));
    };
    if sidecar.has_exited() {
        return Err(AppError::Transcription(String::from(
            "qwen sidecar exited before the request",
        )));
    }
    match sidecar.transcribe(id, wav_path, language, context) {
        Ok(text) => {
            info!(
                "[stt] qwen_sidecar_completed {}",
                serde_json::json!({
                    "id": id,
                    "elapsed_ms": started.elapsed().as_millis(),
                })
            );
            Ok(text)
        }
        Err(failure) => {
            warn!(
                "[stt] qwen_sidecar_failed {}",
                serde_json::json!({"id": id, "reason": failure.reason()})
            );
            // Kill the child under this lock: a late reply can never be
            // matched to the next request.
            *cell = None;
            Err(AppError::Transcription(failure.reason()))
        }
    }
}

/// One transcription through the managed sidecar: the rung the offline
/// ladder calls when parakeet declines a clip. The interpreter resolves
/// through [`qwen_python_executable`]; nothing installed means the rung
/// is off and the ladder falls to the whisper rung.
pub(crate) fn transcribe_wav(
    root_dir: &Path,
    model_id: &str,
    wav_path: &Path,
    language: Option<&str>,
    context: Option<&str>,
) -> Result<String, AppError> {
    let python = qwen_python_executable().ok_or_else(|| {
        AppError::Transcription(String::from(
            "qwen venv is not installed (./ensure-python-env.sh --qwen)",
        ))
    })?;
    transcribe_with(
        &python,
        &qwen_script_path(root_dir),
        model_id,
        wav_path,
        language,
        context,
    )
}

/// Whether the rung could engage at all: interpreter plus script present.
/// true does not transcribe anything; it only lets the ladder log at
/// attempt time which rung is live.
pub(crate) fn rung_available(root_dir: &Path) -> bool {
    qwen_python_executable().is_some() && qwen_script_path(root_dir).exists()
}

/// The offline ladder's complete rung order: qwen leads long clips (the
/// accuracy rung), parakeet leads everything else (the speed rung), each
/// MLX rung falls through to the next on a `None` outcome (rung off,
/// language ineligible, missing venv, sidecar failure, empty text), and
/// whisper stays the final fallback. Rung outcomes surface lazily, so a
/// clip only ever runs the rungs it needs. Split out as a named function
/// so the fallback contract is testable and exists in exactly one place.
pub(crate) fn complete_offline_ladder(
    parakeet: impl FnOnce() -> Option<String>,
    qwen: impl FnOnce() -> Option<String>,
    qwen_leads: bool,
    whisper: impl FnOnce() -> Option<String>,
) -> Option<String> {
    if qwen_leads {
        qwen().or_else(parakeet).or_else(whisper)
    } else {
        parakeet().or_else(qwen).or_else(whisper)
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::io::Write as _;
    use std::path::{Path, PathBuf};
    use std::sync::Mutex;
    use std::sync::mpsc;
    use std::time::{Duration, Instant};

    use super::{
        AppError, QWEN_LEADS_CLIP_THRESHOLD, QWEN_MODEL_ID, QWEN_REQUEST_TIMEOUT,
        QWEN_SIDECAR_SCRIPT, QWEN_STARTUP_TIMEOUT, SidecarFailure, complete_offline_ladder,
        parse_qwen_enabled, parse_ready_line, parse_transcribe_reply, qwen_language_eligible,
        qwen_runs_first, qwen_script_path, transcribe_with, vocabulary_context,
        wait_for_sidecar_reply,
    };

    /// A speak-alike child for protocol tests, stdlib-only so any python3
    /// on the machine can run it. The mode is baked into the written file:
    /// * `echo`: emit ready, answer every transcribe with stub text.
    /// * `die_mid_request`: emit ready, exit on the first request.
    /// * `silent`: emit nothing (startup and requests must time out).
    const STUB_SCRIPT: &str = r#"import json, sys, time
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
if MODE == "echo":
    emit({"type": "ready", "model": "stub"})
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("type") == "stop":
            sys.exit(0)
        if req.get("type") == "transcribe":
            emit({"type": "result", "id": req.get("id"), "text": "stub qwen text", "speech": True, "language": "English"})
elif MODE == "die_mid_request":
    emit({"type": "ready", "model": "stub"})
    for line in sys.stdin:
        sys.exit(3)
elif MODE == "silent":
    time.sleep(600)
"#;

    /// The slot is process-wide, so slot-touching tests serialize on this
    /// lock instead of racing each other.
    static SLOT_GUARD: Mutex<()> = Mutex::new(());

    fn slot_guard() -> &'static Mutex<()> {
        &SLOT_GUARD
    }

    fn python3() -> PathBuf {
        PathBuf::from("python3")
    }

    /// Stub sidecars do not read the model id, but the exchange passes
    /// one; any string exercises the spawn-time env plumbing.
    const STUB_MODEL_ID: &str = "mlx-community/Qwen3-ASR-0.6B-bf16";

    fn write_stub(dir: &Path, mode: &str) -> PathBuf {
        let path = dir.join(format!("stub_sidecar_{mode}.py"));
        let mut file = fs::File::create(&path).unwrap();
        file.write_all(format!("MODE = {mode:?}\n").as_bytes())
            .unwrap();
        file.write_all(STUB_SCRIPT.as_bytes()).unwrap();
        path
    }

    /// Name the stub the production script name so `transcribe_with`'s
    /// script check exercises the real resolution path.
    fn write_named_stub(dir: &Path, mode: &str) -> PathBuf {
        let path = dir.join(QWEN_SIDECAR_SCRIPT);
        let mut file = fs::File::create(&path).unwrap();
        file.write_all(format!("MODE = {mode:?}\n").as_bytes())
            .unwrap();
        file.write_all(STUB_SCRIPT.as_bytes()).unwrap();
        path
    }

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "bolo-qwen-test-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|since| since.as_nanos())
                .unwrap_or_default()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn fake_wav(dir: &Path) -> PathBuf {
        let wav = dir.join("clip.wav");
        fs::write(&wav, b"fake wav bytes for the stub").unwrap();
        wav
    }

    #[test]
    fn enabled_parses_off_vocabulary_and_defaults_on() {
        assert!(parse_qwen_enabled(None));
        assert!(parse_qwen_enabled(Some("")));
        assert!(parse_qwen_enabled(Some("auto")));
        assert!(parse_qwen_enabled(Some("on")));
        assert!(parse_qwen_enabled(Some("1")));
        assert!(parse_qwen_enabled(Some("true")));
        assert!(parse_qwen_enabled(Some("nonsense")));
        assert!(!parse_qwen_enabled(Some("off")));
        assert!(!parse_qwen_enabled(Some("OFF")));
        assert!(!parse_qwen_enabled(Some("false")));
        assert!(!parse_qwen_enabled(Some("0")));
        assert!(!parse_qwen_enabled(Some("no")));
    }

    /// The rung covers exactly the language families the 2026-10-18
    /// benchmark measured (English, Hindi) plus auto-detect; any other
    /// explicit hint stays whisper's, the conservative side.
    #[test]
    fn rung_engages_for_english_hindi_and_auto_only() {
        assert!(qwen_language_eligible("en-US"));
        assert!(qwen_language_eligible("en-IN"));
        assert!(qwen_language_eligible("en"));
        assert!(qwen_language_eligible(" EN "));
        assert!(qwen_language_eligible("hi"));
        assert!(qwen_language_eligible("hi-IN"));
        assert!(qwen_language_eligible("auto"));
        assert!(qwen_language_eligible("AUTO"));
        // Empty means detect, matching local_whisper::whisper_language.
        assert!(qwen_language_eligible(""));
        assert!(!qwen_language_eligible("de-DE"));
        assert!(!qwen_language_eligible("fr"));
        assert!(!qwen_language_eligible("es-MX"));
    }

    /// Long clips lead with qwen; short clips keep parakeet's speed.
    #[test]
    fn qwen_leads_only_at_the_long_clip_threshold() {
        assert!(!qwen_runs_first(Duration::from_secs(5)));
        assert!(!qwen_runs_first(Duration::from_millis(5_999)));
        assert!(qwen_runs_first(Duration::from_secs(6)));
        assert!(qwen_runs_first(Duration::from_secs(60)));
    }

    #[test]
    fn production_threshold_stays_at_six_seconds() {
        assert_eq!(QWEN_LEADS_CLIP_THRESHOLD, Duration::from_secs(6));
    }

    #[test]
    fn vocabulary_context_mirrors_the_cloud_rung_terms() {
        assert_eq!(vocabulary_context(&[]), None);
        let terms: Vec<String> = vec![String::from("Quilter"), String::from("apostle")];
        assert_eq!(
            vocabulary_context(&terms).as_deref(),
            Some("Vocabulary: Quilter, apostle.")
        );
        // The cloud rung caps its prompt at the first 50 terms; the
        // qwen context keeps the same cap.
        let many: Vec<String> = (0..60).map(|index| format!("term{index}")).collect();
        let context = vocabulary_context(&many).unwrap();
        assert!(context.contains("term49."));
        assert!(!context.contains("term50"));
    }

    #[test]
    fn script_path_joins_the_app_root() {
        let dir = temp_dir("script-path");
        assert_eq!(qwen_script_path(&dir), dir.join(QWEN_SIDECAR_SCRIPT));
        assert_eq!(QWEN_MODEL_ID, "mlx-community/Qwen3-ASR-0.6B-bf16");
    }

    /// Live probe against a synthetic cache root: the directory-name
    /// mapping and the file-shape gate, without touching the process
    /// environment or the real HF cache.
    #[test]
    fn model_present_answer_depends_only_on_the_snapshot_layout() {
        let cache = temp_dir("presence");
        // The shipped repo id under an empty cache root is absent, and
        // an unrelated repo never resolves either.
        assert!(!super::model_present_in(&cache, QWEN_MODEL_ID));
        assert!(!super::model_present_in(&cache, "bogus-org/Bogus-ASR"));
        // The directory name mapping is what the HF cache uses.
        let revision = cache
            .join(format!("models--{}", QWEN_MODEL_ID.replace('/', "--")))
            .join("snapshots")
            .join("deadbeef");
        fs::create_dir_all(&revision).unwrap();
        // config alone is not enough: the weights file must exist.
        fs::write(revision.join("config.json"), b"{}").unwrap();
        assert!(!super::model_present_in(&cache, QWEN_MODEL_ID));
        // With real-size weights next to it, the snapshot resolves.
        fs::write(revision.join("model.safetensors"), [0_u8; 1_048_577]).unwrap();
        assert!(super::model_present_in(&cache, QWEN_MODEL_ID));
        // A truncated weights file does not count as a usable snapshot.
        fs::write(revision.join("model.safetensors"), [0_u8; 1024]).unwrap();
        assert!(!super::model_present_in(&cache, QWEN_MODEL_ID));
    }

    #[test]
    fn ready_line_recognizes_exactly_the_handshake() {
        assert!(parse_ready_line(r#"{"type":"ready","model":"x"}"#));
        assert!(!parse_ready_line(r#"{"type":"result","id":1,"text":""}"#));
        assert!(!parse_ready_line("not json"));
        assert!(!parse_ready_line(r#"{"type":"error"}"#));
    }

    #[test]
    fn transcribe_reply_matches_ids_error_messages_and_language_field() {
        assert_eq!(
            parse_transcribe_reply(
                r#"{"type":"result","id":7,"text":"hello","speech":true,"language":"English"}"#,
                7
            ),
            Some(Ok(String::from("hello")))
        );
        assert_eq!(
            parse_transcribe_reply(r#"{"type":"result","id":7,"text":"","speech":false}"#, 7),
            Some(Ok(String::new()))
        );
        assert_eq!(
            parse_transcribe_reply(r#"{"type":"result","id":6,"text":"stale"}"#, 7),
            None
        );
        assert_eq!(
            parse_transcribe_reply(r#"{"type":"error","id":7,"message":"wav read failed"}"#, 7),
            Some(Err(String::from("wav read failed")))
        );
        assert_eq!(parse_transcribe_reply("garbage", 7), None);
        assert_eq!(parse_transcribe_reply(r#"{"type":"gossip"}"#, 7), None);
    }

    #[test]
    fn wait_for_reply_times_out_then_reports_exit() {
        let (sender, receiver) = mpsc::channel::<String>();
        let started = Instant::now();
        let outcome = wait_for_sidecar_reply(&receiver, Duration::from_millis(30));
        assert!(
            matches!(outcome, Err(SidecarFailure::Timeout)),
            "empty channel must time out"
        );
        assert!(started.elapsed() >= Duration::from_millis(30));
        drop(sender);
        let closed = wait_for_sidecar_reply(&receiver, Duration::from_millis(30));
        assert!(
            matches!(closed, Err(SidecarFailure::Exited)),
            "closed channel must report exit, not timeout"
        );
    }

    #[test]
    fn child_serves_ready_then_transcribe_round_trips() {
        let dir = temp_dir("roundtrip");
        let stub = write_stub(&dir, "echo");
        let wav = fake_wav(&dir);
        let mut sidecar = super::QwenSidecar::start(&python3(), &stub, STUB_MODEL_ID).unwrap();
        sidecar.wait_ready_bounded(Duration::from_secs(20)).unwrap();
        let first = sidecar
            .transcribe(1, &wav, Some("en-IN"), Some("Vocabulary: Bolo."))
            .unwrap();
        let second = sidecar.transcribe(2, &wav, None, None).unwrap();
        assert_eq!(first, "stub qwen text");
        assert_eq!(second, "stub qwen text");
        drop(sidecar);
    }

    #[test]
    fn missing_script_fails_without_spawning_a_child() {
        let dir = temp_dir("missing-script");
        let outcome = transcribe_with(
            &python3(),
            &dir.join("nope.py"),
            STUB_MODEL_ID,
            &fake_wav(&dir),
            None,
            None,
        );
        let message = match outcome {
            Err(AppError::Transcription(message)) => message,
            other => panic!("expected a transcription error, got {other:?}"),
        };
        assert!(message.contains("missing"), "unexpected error: {message}");
    }

    /// The ladder-fallback contract at the rung level: a sidecar that dies
    /// mid-request returns `Err` (the ladder then consults whisper), and
    /// the dead child is retired so the next attempt meets a fresh spawn.
    #[test]
    fn dead_child_falls_back_then_the_slot_respawns() {
        let _guard = slot_guard().lock().unwrap();
        let wav = fake_wav(&temp_dir("die-mid.shared-wav"));
        // Warm the slot with a healthy child.
        let healthy = temp_dir("die-mid-healthy");
        let healthy_stub = write_named_stub(&healthy, "echo");
        let warm = transcribe_with(
            &python3(),
            &healthy_stub,
            STUB_MODEL_ID,
            &wav,
            Some("en-IN"),
            None,
        )
        .expect("healthy stub must serve");
        assert_eq!(warm, "stub qwen text");
        // Retire it (test stand-in for a crash between dictations), then
        // point the rung at a stub that dies mid-request: the request
        // must fail through the restart path, ending with the dead child
        // retired, never hanging or answering with stale text.
        super::retire_sidecar("test setup: simulating a crash between dictations");
        let dying = temp_dir("die-mid-dying");
        let dying_stub = write_named_stub(&dying, "die_mid_request");
        let died = transcribe_with(
            &python3(),
            &dying_stub,
            STUB_MODEL_ID,
            &wav,
            Some("hi"),
            None,
        );
        assert!(
            died.is_err(),
            "a sidecar that dies mid-request must fall the ladder back"
        );
        // The restart loop brings a healthy rung back on the next call.
        let revived = temp_dir("die-mid-revived");
        let revived_stub = write_named_stub(&revived, "echo");
        let revived = transcribe_with(&python3(), &revived_stub, STUB_MODEL_ID, &wav, None, None)
            .expect("restart loop must return a fresh child");
        assert_eq!(revived, "stub qwen text");
        super::retire_sidecar("test teardown");
    }

    #[test]
    fn silent_child_fails_the_bounded_handshake() {
        let dir = temp_dir("silent");
        let stub = write_stub(&dir, "silent");
        let mut sidecar = super::QwenSidecar::start(&python3(), &stub, STUB_MODEL_ID).unwrap();
        let started = Instant::now();
        let outcome = sidecar.wait_ready_bounded(Duration::from_millis(300));
        assert!(matches!(outcome, Err(SidecarFailure::Timeout)));
        assert!(started.elapsed() < Duration::from_secs(5));
        drop(sidecar);
    }

    #[test]
    fn stalled_reply_hits_the_request_deadline_not_forever() {
        let dir = temp_dir("stalled");
        // The silent stub never answers at all: neither ready nor result.
        // The request exchange must respect one fixed deadline and fail
        // with the timeout instead of blocking, which this bounds at
        // 300 ms even though the production constant is 10 s.
        let stub = write_stub(&dir, "silent");
        let mut sidecar = super::QwenSidecar::start(&python3(), &stub, STUB_MODEL_ID).unwrap();
        let outcome = sidecar.transcribe_bounded(
            1,
            Path::new("/nowhere.wav"),
            None,
            None,
            Duration::from_millis(300),
        );
        assert!(matches!(outcome, Err(SidecarFailure::Timeout)));
        drop(sidecar);
    }

    #[test]
    fn production_budgets_stay_at_the_documented_bounds() {
        assert_eq!(QWEN_REQUEST_TIMEOUT, Duration::from_secs(10));
        assert_eq!(QWEN_STARTUP_TIMEOUT, Duration::from_secs(30));
    }

    /// Live smoke, ignored by default: needs the real qwen venv, the
    /// HF-cached weights, and `BOLO_QWEN_SMOKE_WAV` pointing at a
    /// 16 kHz mono clip. Run on a machine with the venv:
    /// `BOLO_QWEN_PYTHON=<venv python> BOLO_QWEN_SMOKE_WAV=<clip> cargo test qwen -- --ignored`.
    /// The expected transcript is the benchmark's archived output
    /// (results/rows-qwen.json) for the cleanup_prose clip: byte-identical
    /// through this sidecar, so this doubles as a check that the shipped
    /// path reproduces the measured model exactly.
    #[test]
    #[ignore = "needs the real qwen venv, model weights, and a clip"]
    fn live_venv_transcribes_a_benchmark_clip() {
        let clip = std::env::var("BOLO_QWEN_SMOKE_WAV")
            .expect("set BOLO_QWEN_SMOKE_WAV to a 16 kHz mono wav");
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let expected = "If I'm making typos or mistakes, it cleans it up.";
        let record = {
            let _guard = slot_guard().lock().unwrap();
            super::retire_sidecar("live smoke wants its own child");
            let text =
                super::transcribe_wav(&root, QWEN_MODEL_ID, Path::new(&clip), Some("en-US"), None)
                    .expect("the live venv must serve the benchmark clip");
            super::retire_sidecar("live smoke teardown");
            text
        };
        assert_eq!(record, expected);
    }

    /// The ladder contract, short-clip order: a parakeet answer wins
    /// outright and neither qwen nor whisper runs.
    #[test]
    fn ladder_short_clip_skips_qwen_and_whisper_when_parakeet_answers() {
        let mut qwen_consulted = false;
        let mut whisper_consulted = false;
        let outcome = complete_offline_ladder(
            || Some(String::from("parakeet text")),
            || {
                qwen_consulted = true;
                Some(String::from("qwen text"))
            },
            false,
            || {
                whisper_consulted = true;
                Some(String::from("whisper text"))
            },
        );
        assert_eq!(outcome.as_deref(), Some("parakeet text"));
        assert!(
            !qwen_consulted,
            "qwen must not run after a parakeet answer on a short clip"
        );
        assert!(
            !whisper_consulted,
            "whisper must not run after a parakeet answer"
        );
    }

    /// The ladder contract, short clips: parakeet empty hands the clip to
    /// qwen, and qwen's answer keeps whisper untouched.
    #[test]
    fn ladder_short_clip_falls_parakeet_then_qwen_before_whisper() {
        let mut whisper_consulted = false;
        let outcome = complete_offline_ladder(
            || None,
            || Some(String::from("qwen text")),
            false,
            || {
                whisper_consulted = true;
                Some(String::from("whisper text"))
            },
        );
        assert_eq!(outcome.as_deref(), Some("qwen text"));
        assert!(!whisper_consulted);
        // Both MLX rungs empty means the ladder reaches whisper.
        let fallen = complete_offline_ladder(
            || None,
            || None,
            false,
            || Some(String::from("whisper text")),
        );
        assert_eq!(fallen.as_deref(), Some("whisper text"));
        let empty = complete_offline_ladder(|| None, || None, false, || None);
        assert_eq!(
            empty, None,
            "every rung empty means the ladder yields nothing"
        );
    }

    /// The ladder contract, long clips: qwen leads, and a qwen answer
    /// never pays for the parakeet rung either.
    #[test]
    fn ladder_long_clip_leads_with_qwen() {
        let mut parakeet_consulted = false;
        let mut whisper_consulted = false;
        let outcome = complete_offline_ladder(
            || {
                parakeet_consulted = true;
                Some(String::from("parakeet text"))
            },
            || Some(String::from("qwen text")),
            true,
            || {
                whisper_consulted = true;
                Some(String::from("whisper text"))
            },
        );
        assert_eq!(outcome.as_deref(), Some("qwen text"));
        assert!(
            !parakeet_consulted,
            "a long clip answered by qwen must not also transcribe parakeet"
        );
        assert!(!whisper_consulted);
        // Qwen declining a long clip falls to parakeet, then whisper.
        let parakeet_won = complete_offline_ladder(
            || Some(String::from("parakeet text")),
            || None,
            true,
            || Some(String::from("whisper text")),
        );
        assert_eq!(parakeet_won.as_deref(), Some("parakeet text"));
        let whisper_won = complete_offline_ladder(
            || None,
            || None,
            true,
            || Some(String::from("whisper text")),
        );
        assert_eq!(whisper_won.as_deref(), Some("whisper text"));
    }
}

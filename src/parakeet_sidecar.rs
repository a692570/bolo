//! The parakeet rung: a managed Python sidecar (parakeet_sidecar.py) that
//! keeps `mlx-community/parakeet-tdt-0.6b-v3` resident in a child process
//! via parakeet-mlx on Apple Silicon Metal, so the top of Bolo's offline
//! STT ladder is a pipe round-trip instead of a cold model load.
//!
//! Lifecycle mirrors the accessibility daemon (`main.rs`): the runtime
//! spawns the child lazily (first local transcription), confirms the
//! `{"type":"ready"}` handshake before any request goes out, and answers
//! one `{"type":"transcribe","id":N,"wav_path":...}` request with one
//! `{"type":"result",...}` line. A sidecar that dies or stalls past the
//! per-request budget is dropped from its slot; the next request spawns
//! a fresh child, so recovery costs at most one attempt: the same
//! restart loop the daemon's supervisor implements, but on demand (a
//! resident supervisor would keep ~1.6 GB of transcription memory hot
//! forever, which is only worth paying while dictating).
//!
//! The interpreter resolves through `BOLO_PARAKEET_PYTHON`, then the
//! managed `~/.bolo/parakeet-venv/bin/python3` (created by
//! `ensure-python-env.sh --parakeet`, separate from the Python 3.9
//! helper venv because parakeet-mlx 0.5.3 needs >= 3.10). With neither
//! present the rung reports itself unavailable and the ladder falls to
//! the existing whisper.cpp path unchanged.

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

/// The model the benchmark (laneG, 2026-10-17) measured at 13.6% median
/// WER on Bolo's 134-clip harness: better than the cloud nova-3 batch
/// default (14.3%) at 0.086 s median per clip, English-trained.
pub(crate) const PARAKEET_MODEL_ID: &str = "mlx-community/parakeet-tdt-0.6b-v3";
/// The sidecar script, resolved against the app's root dir (repo root
/// in a source run, Resources in the bundle): the same convention the
/// accessibility daemon uses.
pub(crate) const PARAKEET_SIDECAR_SCRIPT: &str = "parakeet_sidecar.py";
/// One transcription round-trip. laneG measured 0.086 s median per clip
/// and the longest clips (32 s of audio) transcribed in a few hundred ms,
/// so 10 s is generous while still bounding a wedged inference hard.
pub(crate) const PARAKEET_REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
/// Bounded startup: the venv python must import MLX, load the 2.4 GB
/// weights from the warm HF cache (~0.6 s) and emit `ready`. The laneG
/// cold start (process spawn to first transcript) was 0.755 s; a fresh
/// process paying interpreter startup and imports lands a few seconds
/// under this, which leaves room for a first-ever cold boot instead of
/// failing at ten. A load that outruns this is abandoned and retried on
/// the next dictation.
pub(crate) const PARAKEET_STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
/// Managed venv created by `ensure-python-env.sh --parakeet`; a constant
/// apart from the helper venv because parakeet needs Python >= 3.10 and
/// the helper venv runs Python 3.9.
const PARAKEET_VENV_PYTHON: &str = ".bolo/parakeet-venv/bin/python3";

/// Parse `BOLO_LOCAL_PARAKEET`: the parakeet rung is on unless explicitly
/// disabled. Recognizes the same off vocabulary as [`crate::load_bool_env`]
/// around a default of enabled: `off`/`false`/`0`/`no` disable the rung,
/// everything else (including the absent value) keeps the ladder's top
/// rung active, so the default matches `BOLO_LOCAL_STT=auto` behavior.
pub(crate) fn parse_parakeet_enabled(value: Option<&str>) -> bool {
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

/// The parakeet model is English-trained: laneG measured a Devanagari
/// clip coming back as English-shaped words ("Jeff.") instead of failing.
/// The rung therefore engages only for English language hints; `auto`
/// steers to the multilingual whisper rung, which detects instead of
/// anglicizing.
pub(crate) fn parakeet_language_eligible(configured: &str) -> bool {
    let base = configured.trim().to_ascii_lowercase();
    if base.is_empty() || base == "auto" {
        return false;
    }
    base.split(['-', '_']).next() == Some("en")
}

/// The interpreter the sidecar runs under: `BOLO_PARAKEET_PYTHON` pinned
/// by the user, else the managed parakeet venv. `None` disables the rung
/// (the venv is a deliberate, one-time `--parakeet` install).
pub(crate) fn parakeet_python_executable() -> Option<PathBuf> {
    if let Some(value) = crate::load_env_value("BOLO_PARAKEET_PYTHON") {
        let path = PathBuf::from(value);
        if path.is_file() {
            return Some(path);
        }
        warn!(
            "[stt] parakeet_python_unusable {}",
            serde_json::json!({"path": path.display().to_string()})
        );
        return None;
    }
    let managed = crate::home_path(PARAKEET_VENV_PYTHON);
    if managed.is_file() {
        return Some(managed);
    }
    None
}

/// Path of the sidecar script inside the app's root dir.
pub(crate) fn parakeet_script_path(root_dir: &Path) -> PathBuf {
    root_dir.join(PARAKEET_SIDECAR_SCRIPT)
}

/// A fresh temp WAV path for one parakeet attempt: names collide never
/// (pid plus nanoseconds), land in the system temp dir the tests use,
/// and the attempt's worker removes the file after the exchange.
pub(crate) fn temp_recording_path() -> Option<PathBuf> {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_nanos();
    Some(std::env::temp_dir().join(format!("bolo-parakeet-{}-{nanos}.wav", std::process::id())))
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
/// accessibility daemon's shape: one reader thread owns stdout and the
/// request path reads parsed replies through a bounded channel.
struct ParakeetSidecar {
    child: Child,
    stdin: ChildStdin,
    replies: Receiver<String>,
}

impl Drop for ParakeetSidecar {
    fn drop(&mut self) {
        // Kill first, then reap: no zombie, and the reader thread sees EOF
        // even if the child was wedged mid-request.
        let _kill_result = self.child.kill();
        let _wait_result = self.child.wait();
    }
}

impl ParakeetSidecar {
    /// Spawn the sidecar under `python` without touching the process-wide
    /// slot, so a slow warm-up can never hold the slot's lock.
    fn start(python: &Path, script: &Path) -> Result<Self, String> {
        if !script.exists() {
            return Err(format!("{} is missing", script.display()));
        }
        let mut child = Command::new(python)
            .arg(script)
            .env("BOLO_PARENT_PID", std::process::id().to_string())
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
            .name(String::from("bolo-parakeet-reader"))
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
        self.wait_ready_bounded(PARAKEET_STARTUP_TIMEOUT)
    }

    /// [`ParakeetSidecar::wait_ready`] with an explicit budget.
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
                "[stt] parakeet_unexpected_prewrite_line {}",
                serde_json::json!({"line": line})
            );
        }
    }

    /// Send one transcribe request and return the sidecar's text for
    /// exactly this `id`, parsed by [`parse_transcribe_reply`], bounded
    /// by the request constant.
    fn transcribe(&mut self, id: u64, wav_path: &Path) -> Result<String, SidecarFailure> {
        self.transcribe_bounded(id, wav_path, PARAKEET_REQUEST_TIMEOUT)
    }

    /// [`ParakeetSidecar::transcribe`] with an explicit budget.
    fn transcribe_bounded(
        &mut self,
        id: u64,
        wav_path: &Path,
        budget: Duration,
    ) -> Result<String, SidecarFailure> {
        let line = serde_json::json!({
            "type": "transcribe",
            "id": id,
            "wav_path": wav_path.display().to_string(),
        })
        .to_string();
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
                        "[stt] parakeet_unexpected_reply_line {}",
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
/// `None` when the line is malformed, an id mismatch, or gossip.
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
/// child; the slot outlives App instances exactly like the daemon's.
static PARAKEET_SIDECAR: OnceLock<Mutex<Option<ParakeetSidecar>>> = OnceLock::new();

fn parakeet_sidecar_cell() -> &'static Mutex<Option<ParakeetSidecar>> {
    PARAKEET_SIDECAR.get_or_init(|| Mutex::new(None))
}

/// Spawn a sidecar and confirm the ready handshake before it goes anywhere
/// near the slot, so callers never wait on interpreter startup twice.
fn spawn_ready_sidecar(python: &Path, script: &Path) -> Result<ParakeetSidecar, String> {
    let mut sidecar = ParakeetSidecar::start(python, script)?;
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
    let mut cell = parakeet_sidecar_cell()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    match cell.as_mut() {
        Some(sidecar) => !sidecar.has_exited(),
        None => false,
    }
}

/// Store a freshly warmed sidecar when the slot is free; the loser of a
/// race is killed by its own Drop, keeping the single-sidecar invariant.
fn store_sidecar(sidecar: ParakeetSidecar) -> bool {
    let mut cell = parakeet_sidecar_cell()
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
    let mut cell = parakeet_sidecar_cell()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if cell.take().is_some() {
        info!("[stt] parakeet_sidecar_retired {}", reason);
    }
}

/// One transcription through a sidecar the caller resolved: the slot
/// machine plus the exchange. `Err` (missing script, wedged child,
/// timeout) leaves the ladder to fall through to whisper. Budgets use
/// the startup and request constants; the request id is monotonic.
fn transcribe_with(python: &Path, script: &Path, wav_path: &Path) -> Result<String, AppError> {
    if !script.exists() {
        return Err(AppError::Transcription(format!(
            "parakeet sidecar script is missing: {}",
            script.display()
        )));
    }
    // Ensure a live child: prune the exited one, spawn and handshake
    // outside the slot lock so nothing else ever waits on startup.
    if !sidecar_is_running() {
        retire_sidecar("previous child exited");
        match spawn_ready_sidecar(python, script) {
            Ok(sidecar) => {
                if !store_sidecar(sidecar) {
                    info!(
                        "[stt] parakeet_sidecar_start_raced_with_another; keeping the existing child"
                    );
                }
            }
            Err(reason) => {
                warn!("[stt] parakeet_sidecar_start_failed {}", reason);
                return Err(AppError::Transcription(reason));
            }
        }
    }
    let id = REQUEST_ID.fetch_add(1, Ordering::SeqCst);
    let started = Instant::now();
    let mut cell = parakeet_sidecar_cell()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some(sidecar) = cell.as_mut() else {
        return Err(AppError::Transcription(String::from(
            "parakeet sidecar slot is empty",
        )));
    };
    if sidecar.has_exited() {
        return Err(AppError::Transcription(String::from(
            "parakeet sidecar exited before the request",
        )));
    }
    match sidecar.transcribe(id, wav_path) {
        Ok(text) => {
            info!(
                "[stt] parakeet_sidecar_completed {}",
                serde_json::json!({
                    "id": id,
                    "elapsed_ms": started.elapsed().as_millis(),
                })
            );
            Ok(text)
        }
        Err(failure) => {
            warn!(
                "[stt] parakeet_sidecar_failed {}",
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
/// ladder calls first. The interpreter resolves through
/// [`parakeet_python_executable`]; nothing installed means the rung is
/// off and the ladder falls to the whisper rung.
pub(crate) fn transcribe_wav(root_dir: &Path, wav_path: &Path) -> Result<String, AppError> {
    let python = parakeet_python_executable().ok_or_else(|| {
        AppError::Transcription(String::from(
            "parakeet venv is not installed (./ensure-python-env.sh --parakeet)",
        ))
    })?;
    transcribe_with(&python, &parakeet_script_path(root_dir), wav_path)
}

/// Whether the rung could engage at all: interpreter plus script present.
/// true does not transcribe anything; it only lets the ladder log at
/// attempt time which rung is live.
pub(crate) fn rung_available(root_dir: &Path) -> bool {
    parakeet_python_executable().is_some() && parakeet_script_path(root_dir).exists()
}

/// The offline ladder's rung order: the parakeet outcome wins outright,
/// and only a `None` outcome (rung off, non-English hint, missing venv,
/// sidecar failure, empty text) consults the whisper rung. Split out as
/// a named function so the fallback contract is testable and exists in
/// exactly one place.
pub(crate) fn complete_offline_ladder(
    parakeet: Option<String>,
    whisper: impl FnOnce() -> Option<String>,
) -> Option<String> {
    parakeet.or_else(whisper)
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
        AppError, PARAKEET_REQUEST_TIMEOUT, PARAKEET_SIDECAR_SCRIPT, PARAKEET_STARTUP_TIMEOUT,
        SidecarFailure, parakeet_language_eligible, parakeet_script_path, parse_parakeet_enabled,
        parse_ready_line, parse_transcribe_reply, transcribe_with, wait_for_sidecar_reply,
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
    emit({"type": "ready"})
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("type") == "stop":
            sys.exit(0)
        if req.get("type") == "transcribe":
            emit({"type": "result", "id": req.get("id"), "text": "stub parakeet text", "speech": True})
elif MODE == "die_mid_request":
    emit({"type": "ready"})
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
        let path = dir.join(PARAKEET_SIDECAR_SCRIPT);
        let mut file = fs::File::create(&path).unwrap();
        file.write_all(format!("MODE = {mode:?}\n").as_bytes())
            .unwrap();
        file.write_all(STUB_SCRIPT.as_bytes()).unwrap();
        path
    }

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "bolo-parakeet-test-{tag}-{}-{}",
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
    fn parakeet_enabled_parses_off_vocabulary_and_defaults_on() {
        assert!(parse_parakeet_enabled(None));
        assert!(parse_parakeet_enabled(Some("")));
        assert!(parse_parakeet_enabled(Some("auto")));
        assert!(parse_parakeet_enabled(Some("on")));
        assert!(parse_parakeet_enabled(Some("1")));
        assert!(parse_parakeet_enabled(Some("true")));
        assert!(parse_parakeet_enabled(Some("nonsense")));
        assert!(!parse_parakeet_enabled(Some("off")));
        assert!(!parse_parakeet_enabled(Some("OFF")));
        assert!(!parse_parakeet_enabled(Some("false")));
        assert!(!parse_parakeet_enabled(Some("0")));
        assert!(!parse_parakeet_enabled(Some("no")));
    }

    #[test]
    fn rung_engages_for_english_hints_only() {
        assert!(parakeet_language_eligible("en-US"));
        assert!(parakeet_language_eligible("en-IN"));
        assert!(parakeet_language_eligible("en"));
        assert!(parakeet_language_eligible(" EN "));
        assert!(!parakeet_language_eligible("hi"));
        assert!(!parakeet_language_eligible("hi-IN"));
        assert!(!parakeet_language_eligible("auto"));
        assert!(!parakeet_language_eligible(""));
        assert!(!parakeet_language_eligible("de-DE"));
    }

    #[test]
    fn script_path_joins_the_app_root() {
        let dir = temp_dir("script-path");
        assert_eq!(
            parakeet_script_path(&dir),
            dir.join(PARAKEET_SIDECAR_SCRIPT)
        );
    }

    #[test]
    fn ready_line_recognizes_exactly_the_handshake() {
        assert!(parse_ready_line(r#"{"type":"ready","model":"x"}"#));
        assert!(!parse_ready_line(r#"{"type":"result","id":1,"text":""}"#));
        assert!(!parse_ready_line("not json"));
        assert!(!parse_ready_line(r#"{"type":"error"}"#));
    }

    #[test]
    fn transcribe_reply_matches_ids_and_error_messages() {
        assert_eq!(
            parse_transcribe_reply(
                r#"{"type":"result","id":7,"text":"hello","speech":true}"#,
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
        let mut sidecar = super::ParakeetSidecar::start(&python3(), &stub).unwrap();
        sidecar.wait_ready_bounded(Duration::from_secs(20)).unwrap();
        let first = sidecar.transcribe(1, &wav).unwrap();
        let second = sidecar.transcribe(2, &wav).unwrap();
        assert_eq!(first, "stub parakeet text");
        assert_eq!(second, "stub parakeet text");
        drop(sidecar);
    }

    #[test]
    fn missing_script_fails_without_spawning_a_child() {
        let dir = temp_dir("missing-script");
        let outcome = transcribe_with(&python3(), &dir.join("nope.py"), &fake_wav(&dir));
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
        let warm =
            transcribe_with(&python3(), &healthy_stub, &wav).expect("healthy stub must serve");
        assert_eq!(warm, "stub parakeet text");
        // Retire it (test stand-in for a crash between dictations), then
        // point the rung at a stub that dies mid-request: the request
        // must fail through the restart path, ending with the dead child
        // retired, never hanging or answering with stale text.
        super::retire_sidecar("test setup: simulating a crash between dictations");
        let dying = temp_dir("die-mid-dying");
        let dying_stub = write_named_stub(&dying, "die_mid_request");
        let died = transcribe_with(&python3(), &dying_stub, &wav);
        assert!(
            died.is_err(),
            "a sidecar that dies mid-request must fall the ladder back"
        );
        // The restart loop brings a healthy rung back on the next call.
        let revived = temp_dir("die-mid-revived");
        let revived_stub = write_named_stub(&revived, "echo");
        let revived = transcribe_with(&python3(), &revived_stub, &wav)
            .expect("restart loop must return a fresh child");
        assert_eq!(revived, "stub parakeet text");
        super::retire_sidecar("test teardown");
    }

    #[test]
    fn silent_child_fails_the_bounded_handshake() {
        let dir = temp_dir("silent");
        let stub = write_stub(&dir, "silent");
        let mut sidecar = super::ParakeetSidecar::start(&python3(), &stub).unwrap();
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
        let mut sidecar = super::ParakeetSidecar::start(&python3(), &stub).unwrap();
        let outcome =
            sidecar.transcribe_bounded(1, Path::new("/nowhere.wav"), Duration::from_millis(300));
        assert!(matches!(outcome, Err(SidecarFailure::Timeout)));
        drop(sidecar);
    }

    #[test]
    fn production_budgets_stay_at_the_documented_bounds() {
        assert_eq!(PARAKEET_REQUEST_TIMEOUT, Duration::from_secs(10));
        assert_eq!(PARAKEET_STARTUP_TIMEOUT, Duration::from_secs(30));
    }

    /// Live smoke, ignored by default: needs the real parakeet venv, the
    /// HF-cached weights, and `BOLO_PARAKEET_SMOKE_WAV` pointing at a
    /// 16 kHz mono clip. Run on a machine with the venv:
    /// `BOLO_PARAKEET_SMOKE_WAV=<clip> cargo test parakeet -- --ignored`.
    /// The expected transcript is the laneG benchmark's archived output
    /// for the benchmark's first clip (byte-identical through this
    /// sidecar, verified 2026-10-17), so this doubles as a check that
    /// the shipped path reproduces the measured model exactly.
    #[test]
    #[ignore = "needs the real parakeet venv, model weights, and a clip"]
    fn live_venv_transcribes_a_benchmark_clip() {
        let clip = std::env::var("BOLO_PARAKEET_SMOKE_WAV")
            .expect("set BOLO_PARAKEET_SMOKE_WAV to a 16 kHz mono wav");
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let expected = "No, from my end I think everything was completed so free to do whenever.";
        let record = {
            let _guard = slot_guard().lock().unwrap();
            super::retire_sidecar("live smoke wants its own child");
            let text = super::transcribe_wav(&root, Path::new(&clip))
                .expect("the live venv must serve the benchmark clip");
            super::retire_sidecar("live smoke teardown");
            text
        };
        assert_eq!(record, expected);
    }

    #[test]
    fn ladder_skips_whisper_when_parakeet_answers() {
        let mut whisper_consulted = false;
        let outcome = super::complete_offline_ladder(Some(String::from("parakeet text")), || {
            whisper_consulted = true;
            Some(String::from("whisper text"))
        });
        assert_eq!(outcome.as_deref(), Some("parakeet text"));
        assert!(
            !whisper_consulted,
            "whisper must not run after a parakeet answer"
        );
    }

    #[test]
    fn ladder_falls_to_whisper_when_parakeet_yields_nothing() {
        let outcome = super::complete_offline_ladder(None, || Some(String::from("whisper text")));
        assert_eq!(outcome.as_deref(), Some("whisper text"));
        let empty = super::complete_offline_ladder(None, || None);
        assert_eq!(
            empty, None,
            "both rungs empty means the ladder yields nothing"
        );
    }
}

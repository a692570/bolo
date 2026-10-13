//! Local whisper.cpp transcription tier: the last rung of Bolo's STT ladder.
//! Model management downloads ggml files into `~/.bolo/models` with
//! sha256 verification against the published digest, a `.sha256` sidecar
//! recording what landed on disk, and status checks that gate the
//! release-path fallback (missing, unreadable, or wrong-magic models are
//! reported, never silently used).
//! Transcription runs whisper.cpp through whisper-rs (Metal on Apple
//! Silicon), always on a worker thread under a recording-length-derived
//! time budget, so a wedged model can never block release.

use std::ffi::c_int;
use std::fmt;
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::Duration;

use reqwest::blocking::Client;
use sha2::Digest;
use sha2::Sha256;
use tracing::warn;
use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};

use crate::AppError;

/// Sample rate every whisper model expects: 16 kHz mono f32. The pipeline
/// resamples its 48 kHz capture down to this rate before the local tier.
pub(crate) const WHISPER_SAMPLE_RATE: u32 = 16_000;
/// Default offline model: `small` is multilingual, ~466 MB, and fast
/// enough on Apple Silicon to stay inside the fallback budget.
pub(crate) const DEFAULT_MODEL_FILE: &str = "ggml-small.bin";
/// Canonical ggml distribution of whisper.cpp models on Hugging Face.
const MODEL_URL_BASE: &str = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main";
/// Verified 2026-10-13: `HEAD` follows the redirect with 200 and
/// content-length 487,601,967; the digest is the Hugging Face LFS sha256
/// for `ggml-small.bin`, the file's actual content hash.
const GGML_SMALL_SHA256: &str = "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b";
/// Published size of the default model, verified with the digest above.
const GGML_SMALL_SIZE: u64 = 487_601_967;
/// Magic header every ggml model file starts with. The ggml magic is the
/// little-endian serialization of the u32 0x67676d6c, so the first four
/// bytes on disk read "lmgg", not "ggml". Verified against
/// ggml-small.bin on 2026-10-13 (hex dump: 6c6d 6767).
const GGML_MAGIC: &[u8; 4] = b"lmgg";
/// Download progress is reported at each 10% mark of the announced
/// length, 0 and 100 included.
const DOWNLOAD_PROGRESS_STEP: u64 = 10;
/// Download chunk size: 256 KiB keeps the hash, the file write, and the
/// progress ticks in one pass without huge memory spikes.
const DOWNLOAD_CHUNK: usize = 256 * 1024;
/// Hard ceiling on one model download (connect + transfer + tail), so a
/// wedged transfer cannot outlive an app session.
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(1_800);
/// Connection budget before the first byte of a download.
const DOWNLOAD_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// Minimum wall-clock budget for one local transcription, however short
/// the recording: covers the first-time model load (~1-2 s for `small`
/// on Apple Silicon) plus inference.
const LOCAL_STT_MIN_BUDGET: Duration = Duration::from_secs(10);
/// Local transcription budget multiplier: the release path never waits
/// longer than 3x the recording duration for the offline tier.
const LOCAL_STT_BUDGET_MULTIPLE: u32 = 3;
/// Installs whisper.cpp's log hooks exactly once; with no log backend
/// feature enabled this mutes whisper.cpp's stdout chatter, keeping
/// Bolo quiet unless Bolo itself logs.
static WHISPER_LOG_HOOKS: OnceLock<()> = OnceLock::new();

/// Release-path policy for the offline tier, read from `BOLO_LOCAL_STT`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum LocalSttMode {
    /// Run the local tier only after every cloud path failed or returned
    /// nothing. The default.
    Auto,
    /// Skip cloud transcription entirely; dictate offline.
    On,
    /// Never consult the local tier; the pre-offline behavior.
    Off,
}

/// Parse `BOLO_LOCAL_STT`: an absent or unrecognized value stays at
/// `auto`, mirroring how `BOLO_LLM_CLEANUP` defaults.
pub(crate) fn parse_local_stt_mode(value: Option<&str>) -> LocalSttMode {
    match value
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase()
        .as_str()
    {
        "on" => LocalSttMode::On,
        "off" => LocalSttMode::Off,
        _ => LocalSttMode::Auto,
    }
}

/// Map Bolo's STT language hint (`en-IN`, `en-US`, `auto`, `hi`, ...) to
/// the language whisper.cpp expects: the primary ISO tag, lowercased.
/// `auto` and empty mean "detect", which whisper.cpp expresses as no
/// language pin (`None`), the same convention the cloud fallbacks use.
pub(crate) fn whisper_language(configured: &str) -> Option<String> {
    let base = configured.trim().to_ascii_lowercase();
    if base.is_empty() || base == "auto" {
        return None;
    }
    let primary = base.split(['-', '_']).next().unwrap_or_default();
    Some(primary.to_owned())
}

/// The wall-clock budget for one local transcription: 3x the recording
/// duration, floored at 10 s, so a short clip never starves the model
/// load and a hung inference can never wedge the release path longer
/// than the clip itself could justify.
pub(crate) fn local_stt_budget(recording_duration: Duration) -> Duration {
    recording_duration
        .saturating_mul(LOCAL_STT_BUDGET_MULTIPLE)
        .max(LOCAL_STT_MIN_BUDGET)
}

/// Digest metadata for models whose published hash Bolo knows, so a
/// download can be verified byte-for-byte rather than by magic alone.
/// Only the default model carries a verified digest today; custom models
/// named through `BOLO_LOCAL_WHISPER_MODEL` fall back to magic + sidecar.
struct KnownModel {
    sha256: &'static str,
    size: u64,
}

fn known_model(file: &str) -> Option<KnownModel> {
    if file == DEFAULT_MODEL_FILE {
        Some(KnownModel {
            sha256: GGML_SMALL_SHA256,
            size: GGML_SMALL_SIZE,
        })
    } else {
        None
    }
}

/// Everything the download path needs for one model: the target file
/// name, its source URL, and the optional digest/size to verify against.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct OfflineModelSpec {
    pub(crate) file: String,
    pub(crate) url: String,
    pub(crate) expected_sha256: Option<String>,
    pub(crate) expected_size: Option<u64>,
}

/// Resolve the download spec for the configured model file. Known models
/// carry their published sha256 and exact size; anything else resolves
/// against the same whisper.cpp distribution and verifies by magic
/// header only, with the actual digest recorded in the sidecar.
pub(crate) fn offline_model_spec(configured_file: &str) -> OfflineModelSpec {
    let trimmed = configured_file.trim();
    let file = if trimmed.is_empty() {
        DEFAULT_MODEL_FILE
    } else {
        trimmed
    };
    let (expected_sha256, expected_size) = known_model(file).map_or((None, None), |known| {
        (Some(known.sha256.to_owned()), Some(known.size))
    });
    OfflineModelSpec {
        file: file.to_owned(),
        url: format!("{MODEL_URL_BASE}/{file}"),
        expected_sha256,
        expected_size,
    }
}

/// On-disk state of the configured offline model.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ModelStatus {
    /// The file exists, has the ggml magic, and matches the known size.
    Ready,
    /// No file at the expected path.
    Missing,
    /// A file exists but is not usable as this model, with a static
    /// reason suitable for direct logging.
    Invalid(&'static str),
}

impl fmt::Display for ModelStatus {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Ready => write!(formatter, "ready"),
            Self::Missing => write!(formatter, "missing"),
            Self::Invalid(reason) => write!(formatter, "invalid ({reason})"),
        }
    }
}

/// Check the configured model without hashing it: presence, the ggml
/// magic header, and the known-good size when the model is one Bolo
/// published metadata for. The full sha256 is verified while the
/// download streams (see [`download_model`]); after that, magic + size
/// is a complete integrity check of the file whisper will actually load.
pub(crate) fn model_status(models_dir: &Path, model_file: &str) -> ModelStatus {
    let path = models_dir.join(model_file);
    let Ok(metadata) = fs::metadata(&path) else {
        return ModelStatus::Missing;
    };
    if !metadata.is_file() {
        return ModelStatus::Invalid("model path is not a regular file");
    }
    if let Some(known) = known_model(model_file)
        && metadata.len() != known.size
    {
        return ModelStatus::Invalid("model file size does not match the published size");
    }
    let mut magic = [0_u8; GGML_MAGIC.len()];
    if fs::File::open(&path)
        .and_then(|mut file| file.read_exact(&mut magic))
        .is_err()
    {
        return ModelStatus::Invalid("model file could not be read");
    }
    if &magic != GGML_MAGIC {
        return ModelStatus::Invalid("model file does not start with the ggml magic header");
    }
    ModelStatus::Ready
}

/// A fresh blocking HTTP client for model downloads. Bolo's shared client
/// carries the 12 s STT request timeout, which would abort a ~466 MB
/// transfer immediately; the download gets its own client with a connect
/// budget and a 30-minute overall ceiling instead.
pub(crate) fn download_client() -> Result<Client, AppError> {
    let client = Client::builder()
        .connect_timeout(DOWNLOAD_CONNECT_TIMEOUT)
        .timeout(DOWNLOAD_TIMEOUT)
        .build()?;
    Ok(client)
}

/// Lowercase hex encoding of a digest, written by hand so the download
/// path carries no extra dependency for one small, well-tested job.
pub(crate) fn write_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut text = String::with_capacity(bytes.len().saturating_mul(2));
    for byte in bytes {
        text.push(char::from(HEX[usize::from(*byte >> 4)]));
        text.push(char::from(HEX[usize::from(*byte & 0x0f)]));
    }
    text
}

/// Download one model into `models_dir`, verifying as it lands: the
/// stream is hashed while it is written to a `.part` file, the digest is
/// compared against the published sha256 when known, the magic header is
/// checked regardless, and only the verified bytes are renamed into
/// place (rename is atomic on the same filesystem). The actual sha256
/// lands in a `<model>.sha256` sidecar, so audits can compare without
/// rehashing by hand. `progress` receives each 10% mark of the announced
/// content length, 0 and 100 included.
pub(crate) fn download_model(
    client: &Client,
    spec: &OfflineModelSpec,
    models_dir: &Path,
    mut progress: impl FnMut(u8),
) -> Result<PathBuf, AppError> {
    fs::create_dir_all(models_dir)?;
    let part_path = models_dir.join(format!(".{}.part", spec.file));
    let final_path = models_dir.join(&spec.file);

    progress(0);
    let mut response = client.get(&spec.url).send()?.error_for_status()?;
    let announced_total = response.content_length();
    let mut hasher = Sha256::new();
    let mut part_file = fs::File::create(&part_path)?;
    let mut buffer = vec![0_u8; DOWNLOAD_CHUNK];
    let mut written: u64 = 0;
    let mut next_tick: u64 = DOWNLOAD_PROGRESS_STEP;
    loop {
        let read = response.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        part_file.write_all(&buffer[..read])?;
        written = written.saturating_add(read as u64);
        if let Some(total) = announced_total.or(spec.expected_size) {
            let percent = written.saturating_mul(100) / total;
            while next_tick <= percent {
                progress(u8::try_from(next_tick).unwrap_or(100));
                next_tick = next_tick.saturating_add(DOWNLOAD_PROGRESS_STEP);
            }
        }
    }
    part_file.flush()?;

    let bad_model = |reason: String, part: &Path| {
        let discarded = fs::remove_file(part);
        drop(discarded);
        Err(AppError::Transcription(reason))
    };

    let mut magic = [0_u8; GGML_MAGIC.len()];
    let magic_read = fs::File::open(&part_path)
        .and_then(|mut file| file.read_exact(&mut magic))
        .is_ok();
    if !magic_read || &magic != GGML_MAGIC {
        return bad_model(
            format!(
                "downloaded model {} does not start with the ggml magic header",
                spec.file
            ),
            &part_path,
        );
    }
    let digest = hasher.finalize();
    let computed_hex = write_hex(&digest);
    if let Some(expected) = spec.expected_sha256.as_deref()
        && !computed_hex.eq_ignore_ascii_case(expected)
    {
        return bad_model(
            format!("downloaded model {} failed sha256 verification", spec.file),
            &part_path,
        );
    }
    fs::rename(&part_path, &final_path)?;
    let sidecar = fs::write(
        models_dir.join(format!("{}.sha256", spec.file)),
        format!("{computed_hex}\n"),
    );
    if let Err(error) = sidecar {
        // The verified model is already in place; the sidecar is a record
        // for audits, so a failure here does not invalidate the download.
        warn!("offline model sidecar write failed: {error}");
    }
    progress(100);
    Ok(final_path)
}

/// The lazily loaded whisper context plus the language pinned from
/// Bolo's settings. The context is loaded once and kept: ~500 MB of
/// resident memory on an always-on personal machine, traded for a
/// sub-second warm path, exactly like the on-device model Willow keeps
/// resident. A re-download replaces the file on disk; the App resets
/// this cache so the next dictation loads the fresh bytes.
pub(crate) struct LocalWhisper {
    model_path: PathBuf,
    language: Option<String>,
    context: Mutex<Option<WhisperContext>>,
}

impl LocalWhisper {
    /// Create the engine shell. Nothing is loaded yet: the model load is
    /// a first-dictation cost, paid inside the transcription budget.
    pub(crate) const fn new(model_path: PathBuf, language: Option<String>) -> Self {
        Self {
            model_path,
            language,
            context: Mutex::new(None),
        }
    }

    /// Transcribe 16 kHz mono f32 samples and return the trimmed text.
    /// The context is loaded on first use under its lock, and a state is
    /// created per call, so two dictations can never interleave their
    /// segment counters; the context itself (the expensive part) stays
    /// cached for the process lifetime.
    pub(crate) fn transcribe(&self, samples_16k: &[f32]) -> Result<String, AppError> {
        let Ok(mut cached) = self.context.lock() else {
            return Err(AppError::PoisonedMutex(String::from(
                "local whisper context",
            )));
        };
        if cached.is_none() {
            let _hooks_installed = WHISPER_LOG_HOOKS.get_or_init(whisper_rs::install_logging_hooks);
            let loaded =
                WhisperContext::new_with_params(&self.model_path, WhisperContextParameters::new())
                    .map_err(|error| {
                        AppError::Transcription(format!(
                            "local whisper model failed to load: {error}"
                        ))
                    })?;
            *cached = Some(loaded);
        }
        let context = cached.as_ref().ok_or_else(|| {
            AppError::Transcription(String::from("local whisper context is absent"))
        })?;
        let mut state = context.create_state().map_err(|error| {
            AppError::Transcription(format!("local whisper state failed: {error}"))
        })?;
        let mut params = FullParams::new(SamplingStrategy::Greedy { best_of: 1 });
        params.set_language(self.language.as_deref());
        params.set_n_threads(whisper_threads());
        params.set_print_progress(false);
        params.set_print_realtime(false);
        params.set_print_special(false);
        params.set_print_timestamps(false);
        state
            .full(params, samples_16k)
            .map_err(|error| AppError::Transcription(format!("local whisper failed: {error}")))?;
        let segment_count = state.full_n_segments();
        let mut text = String::new();
        for index in 0..segment_count {
            let Some(segment) = state.get_segment(index) else {
                continue;
            };
            match segment.to_str_lossy() {
                Ok(part) => {
                    let part = part.trim();
                    if part.is_empty() {
                        continue;
                    }
                    if !text.is_empty() {
                        text.push(' ');
                    }
                    text.push_str(part);
                }
                Err(error) => warn!("local whisper segment unreadable: {error}"),
            }
        }
        Ok(text.trim().to_owned())
    }
}

impl fmt::Debug for LocalWhisper {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LocalWhisper")
            .field("model_path", &self.model_path)
            .field("language", &self.language)
            .field(
                "context_loaded",
                &matches!(self.context.lock().map(|guard| guard.is_some()), Ok(true)),
            )
            .finish()
    }
}

/// CPU threads for the decode half of inference. Metal handles the
/// encoder on Apple Silicon; this only bounds the CPU fallback, and the
/// cap of 8 keeps parity with whisper.cpp's own default behavior on
/// machines with many cores.
fn whisper_threads() -> c_int {
    let parallel = thread::available_parallelism().map_or(4, usize::from);
    c_int::try_from(parallel.min(8)).unwrap_or(4)
}

/// Run `work` on a dedicated thread and wait at most `budget`. The
/// worker thread holds no locks that release or the event loop need,
/// so a timeout abandons it cheaply: release falls through to its normal
/// failure handling while the detached thread finishes and exits on its
/// own. `label` names the thread and the timeout log line.
pub(crate) fn call_with_budget<T, F>(
    label: &'static str,
    budget: Duration,
    work: F,
) -> Result<T, AppError>
where
    F: FnOnce() -> Result<T, AppError> + Send + 'static,
    T: Send + 'static,
{
    let (sender, receiver) = mpsc::channel();
    let spawn_result = thread::Builder::new()
        .name(format!("bolo-{label}"))
        .spawn(move || {
            let _delivered = sender.send(work());
        });
    if let Err(error) = spawn_result {
        return Err(AppError::Transcription(format!(
            "{label} thread failed to start: {error}"
        )));
    }
    match receiver.recv_timeout(budget) {
        Ok(result) => result,
        Err(RecvTimeoutError::Timeout) => {
            warn!(
                "[stt] {} exceeded its {} ms budget; abandoning it to finish in the background",
                label,
                budget.as_millis()
            );
            Err(AppError::Transcription(format!(
                "{label} exceeded its time budget"
            )))
        }
        Err(RecvTimeoutError::Disconnected) => Err(AppError::Transcription(format!(
            "{label} worker ended without reporting a result"
        ))),
    }
}

/// The cached engine handle as the App stores it.
pub(crate) type SharedLocalWhisper = Arc<LocalWhisper>;

#[cfg(test)]
mod tests {
    use std::fs;
    use std::io::{BufRead, BufReader, Write};
    use std::net::{TcpListener, TcpStream};
    use std::path::PathBuf;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    use super::{
        DEFAULT_MODEL_FILE, GGML_MAGIC, LocalSttMode, LocalWhisper, ModelStatus, OfflineModelSpec,
        call_with_budget, download_client, download_model, local_stt_budget, model_status,
        offline_model_spec, parse_local_stt_mode, whisper_language, write_hex,
    };
    use crate::AppError;
    use sha2::{Digest, Sha256};
    use tracing::warn;

    /// Serve `body` over a bound localhost socket as one HTTP response
    /// per connection, returning the URL the code under test should hit.
    /// Only localhost traffic is generated; nothing leaves the machine.
    fn serve_fixture(body: Vec<u8>) -> Result<String, AppError> {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let port = listener.local_addr().map(|address| address.port())?;
        let fixture_thread = thread::Builder::new()
            .name(String::from("bolo-test-http-fixture"))
            .spawn(move || {
                for stream in listener.incoming() {
                    let Ok(mut stream) = stream else { continue };
                    if let Err(error) = serve_connection(&mut stream, &body) {
                        warn!("test fixture connection failed: {error}");
                    }
                }
            })?;
        // Detached on purpose: the fixture lives for the test process and
        // answers as many connections as the test opens.
        drop(fixture_thread);
        Ok(format!("http://127.0.0.1:{port}/model.bin"))
    }

    /// Answer one connection: drain the request headers, reply with the
    /// fixture body, close.
    fn serve_connection(stream: &mut TcpStream, body: &[u8]) -> Result<(), AppError> {
        let reader = BufReader::new(stream.try_clone()?);
        for line in reader.lines() {
            let line = line?;
            if line.trim().is_empty() {
                break;
            }
        }
        let header = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nContent-Type: application/octet-stream\r\nConnection: close\r\n\r\n",
            body.len()
        );
        stream.write_all(header.as_bytes())?;
        stream.write_all(body)?;
        stream.flush()?;
        Ok(())
    }

    /// A private temp directory per test, so model files never touch the
    /// real `~/.bolo/models`.
    fn temp_models_dir() -> Result<PathBuf, AppError> {
        let dir = std::env::temp_dir().join(format!(
            "bolo-local-whisper-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|since| since.as_nanos())
                .unwrap_or_default()
        ));
        fs::create_dir_all(&dir)?;
        Ok(dir)
    }

    /// Fixture model bytes: ggml magic plus filler to a fixed length.
    fn fixture_model_bytes(len: usize) -> Vec<u8> {
        let mut body = Vec::with_capacity(len);
        body.extend_from_slice(GGML_MAGIC);
        body.resize(len, 0x7f);
        body
    }

    #[test]
    fn local_stt_mode_parses_known_values_and_defaults_to_auto() {
        assert_eq!(parse_local_stt_mode(None), LocalSttMode::Auto);
        assert_eq!(parse_local_stt_mode(Some("")), LocalSttMode::Auto);
        assert_eq!(parse_local_stt_mode(Some("auto")), LocalSttMode::Auto);
        assert_eq!(parse_local_stt_mode(Some("AUTO")), LocalSttMode::Auto);
        assert_eq!(parse_local_stt_mode(Some("on")), LocalSttMode::On);
        assert_eq!(parse_local_stt_mode(Some("Off")), LocalSttMode::Off);
        assert_eq!(parse_local_stt_mode(Some("nonsense")), LocalSttMode::Auto);
    }

    #[test]
    fn whisper_language_strips_regions_and_auto_detects() {
        assert_eq!(whisper_language("en-IN").as_deref(), Some("en"));
        assert_eq!(whisper_language("en-US").as_deref(), Some("en"));
        assert_eq!(whisper_language("en").as_deref(), Some("en"));
        assert_eq!(whisper_language("hi-IN").as_deref(), Some("hi"));
        assert_eq!(whisper_language("DE-DE").as_deref(), Some("de"));
        assert_eq!(whisper_language("auto"), None);
        assert_eq!(whisper_language("  "), None);
        assert_eq!(whisper_language(""), None);
    }

    #[test]
    fn local_stt_budget_scales_and_floors() {
        assert_eq!(
            local_stt_budget(Duration::from_secs(1)),
            Duration::from_secs(10)
        );
        assert_eq!(
            local_stt_budget(Duration::from_secs(3)),
            Duration::from_secs(10)
        );
        assert_eq!(
            local_stt_budget(Duration::from_secs(4)),
            Duration::from_secs(12)
        );
        assert_eq!(
            local_stt_budget(Duration::from_secs(30)),
            Duration::from_secs(90)
        );
        assert_eq!(
            local_stt_budget(Duration::from_secs(0)),
            Duration::from_secs(10)
        );
    }

    #[test]
    fn hex_writer_matches_lowercase_sha256_digest_strings() {
        let digest = Sha256::digest(b"bolo");
        let encoded = write_hex(&digest);
        assert_eq!(encoded.len(), 64);
        assert!(
            encoded
                .chars()
                .all(|ch| ch.is_ascii_hexdigit() && !ch.is_ascii_uppercase())
        );
        let empty_digest = Sha256::digest([]);
        assert_eq!(
            write_hex(&empty_digest),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    /// Comparison helper for the Result-returning tests below: the deny
    /// lints forbid panicking assertions in Result tests, so a mismatch
    /// reports as the test's Err instead. References keep algebraic
    /// values cheaply comparable without the pass-by-value warnings the
    /// deny configuration would emit on a generic taken by value.
    fn check<T: PartialEq + std::fmt::Debug + ?Sized>(
        actual: &T,
        expected: &T,
        what: &str,
    ) -> Result<(), AppError> {
        if actual == expected {
            Ok(())
        } else {
            Err(AppError::Transcription(format!(
                "{what}: expected {expected:?}, got {actual:?}"
            )))
        }
    }

    /// Boolean shorthand of [`check`].
    fn ensure(condition: bool, what: &str) -> Result<(), AppError> {
        if condition {
            Ok(())
        } else {
            Err(AppError::Transcription(String::from(what)))
        }
    }

    #[test]
    fn model_status_walks_missing_invalid_and_ready_states() -> Result<(), AppError> {
        let dir = temp_models_dir()?;
        // Custom model name: magic-only verification.
        let custom_name = "ggml-custom.bin";
        check(
            &model_status(&dir, custom_name),
            &ModelStatus::Missing,
            "absent model reported missing",
        )?;
        fs::write(dir.join(custom_name), b"not a model")?;
        check(
            &model_status(&dir, custom_name),
            &ModelStatus::Invalid("model file does not start with the ggml magic header"),
            "wrong-magic model reported invalid",
        )?;
        fs::write(dir.join(custom_name), GGML_MAGIC)?;
        check(
            &model_status(&dir, custom_name),
            &ModelStatus::Ready,
            "magic-matching custom model ready",
        )?;
        // Known model name: size must match the published bytes.
        fs::write(dir.join(DEFAULT_MODEL_FILE), GGML_MAGIC)?;
        check(
            &model_status(&dir, DEFAULT_MODEL_FILE),
            &ModelStatus::Invalid("model file size does not match the published size"),
            "short known model rejected by size",
        )?;
        // A directory in place of the model reads invalid, not missing.
        fs::create_dir_all(dir.join("ggml-dir.bin"))?;
        check(
            &model_status(&dir, "ggml-dir.bin"),
            &ModelStatus::Invalid("model path is not a regular file"),
            "directory in model path reported invalid",
        )
    }

    #[test]
    fn offline_model_spec_resolves_default_with_digest_and_custom_without() {
        let default = offline_model_spec(DEFAULT_MODEL_FILE);
        assert_eq!(
            default.url,
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin"
        );
        assert_eq!(
            default.expected_sha256.as_deref(),
            Some("1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b")
        );
        assert_eq!(default.expected_size, Some(487_601_967));
        let custom = offline_model_spec("ggml-base.bin");
        assert_eq!(
            custom.url,
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
        );
        assert_eq!(custom.expected_sha256, None);
        assert_eq!(custom.expected_size, None);
        assert_eq!(custom.file, "ggml-base.bin");
        let fallback = offline_model_spec("");
        assert_eq!(fallback.file, DEFAULT_MODEL_FILE);
    }

    #[test]
    fn download_verifies_digest_writes_sidecar_and_reports_deciles() -> Result<(), AppError> {
        let dir = temp_models_dir()?;
        let body = fixture_model_bytes(5 * 1024 * 1024);
        let url = serve_fixture(body.clone())?;
        let spec = OfflineModelSpec {
            file: String::from("ggml-crafted.bin"),
            url,
            expected_sha256: Some(write_hex(&Sha256::digest(&body))),
            expected_size: Some(u64::try_from(body.len()).unwrap_or_default()),
        };
        let ticks: Arc<Mutex<Vec<u8>>> = Arc::new(Mutex::new(Vec::new()));
        let progress_sink = Arc::clone(&ticks);
        let path = download_model(&download_client()?, &spec, &dir, move |percent| {
            if let Ok(mut collected) = progress_sink.lock()
                && collected.last() != Some(&percent)
            {
                collected.push(percent);
            }
        })?;
        check(&fs::read(&path)?, &body, "downloaded bytes")?;
        let bytes = fs::read(&path)?;
        let recorded = fs::read_to_string(dir.join("ggml-crafted.bin.sha256"))?;
        check(
            recorded.trim(),
            write_hex(&Sha256::digest(&bytes)).as_str(),
            "sidecar digest",
        )?;
        check(&path, &dir.join("ggml-crafted.bin"), "download destination")?;
        let collected = ticks
            .lock()
            .map_or_else(|_| Vec::new(), |marks| marks.clone());
        check(&collected.first().copied(), &Some(0), "first progress mark")?;
        check(&collected.last().copied(), &Some(100), "last progress mark")?;
        for decile in [10_u8, 20, 30, 40, 50, 60, 70, 80, 90, 100] {
            ensure(
                collected.contains(&decile),
                &format!("missing progress mark {decile}% in {collected:?}"),
            )?;
        }
        ensure(
            !dir.join(".ggml-crafted.bin.part").exists(),
            "part file survived a successful download",
        )
    }

    #[test]
    fn download_rejects_digest_mismatch_and_leaves_no_model_behind() -> Result<(), AppError> {
        let dir = temp_models_dir()?;
        let body = fixture_model_bytes(64 * 1024);
        // The wrong digest: identical bytes would hash identically, so the
        // reference payload differs by one byte to prove the check has
        // real material to catch.
        let mut other = body.clone();
        if let Some(filler) = other.last_mut() {
            *filler ^= 1;
        }
        let url = serve_fixture(body)?;
        let spec = OfflineModelSpec {
            file: String::from("ggml-mismatched.bin"),
            url,
            expected_sha256: Some(write_hex(&Sha256::digest(&other))),
            expected_size: None,
        };
        let result = download_model(&download_client()?, &spec, &dir, |_| {});
        ensure(result.is_err(), "digest mismatch must fail the download")?;
        ensure(
            !dir.join("ggml-mismatched.bin").exists(),
            "rejected model file must not exist",
        )?;
        ensure(
            !dir.join(".ggml-mismatched.bin.part").exists(),
            "rejected part file must be discarded",
        )?;
        ensure(
            !dir.join("ggml-mismatched.bin.sha256").exists(),
            "rejected download gets no sidecar",
        )
    }

    #[test]
    fn call_with_budget_returns_the_work_result_within_the_deadline() {
        let started = Instant::now();
        let result: Result<String, AppError> =
            call_with_budget("test-fast", Duration::from_secs(5), || {
                Ok(String::from("worked"))
            });
        assert_eq!(result.ok().as_deref(), Some("worked"));
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn call_with_budget_abandons_a_worker_that_outlives_the_budget() {
        let started = Instant::now();
        let result: Result<String, AppError> =
            call_with_budget("test-slow", Duration::from_millis(50), || {
                thread::sleep(Duration::from_secs(2));
                Ok(String::from("too late"))
            });
        let outcome = match result {
            Err(AppError::Transcription(message)) => message,
            Err(other) => format!("unexpected error kind: {other}"),
            Ok(text) => format!("work unexpectedly succeeded: {text}"),
        };
        assert!(
            outcome.contains("budget"),
            "expected a budget error, got {outcome}"
        );
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn engine_shell_constructs_without_touching_disk() {
        // LocalWhisper::new defers the model load to the first
        // transcription, so constructing it never opens the model file;
        // the debug rendering also must not need the context to exist.
        let engine = LocalWhisper::new(
            PathBuf::from("/nonexistent-model-path/ggml-tiny.bin"),
            Some(String::from("en")),
        );
        let rendered = format!("{engine:?}");
        assert!(rendered.contains("context_loaded: false"));
    }
}

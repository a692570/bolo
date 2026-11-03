//! Quiet other audio while the dictation key is held (`BOLO_QUIET_AUDIO`).
//!
//! Press lowers the system output volume and release puts it back. Every
//! volume read and write runs on one worker thread, so the press and release
//! paths only pay for a channel send and the requests are applied strictly in
//! order (a quick second dictation can never read the first one's ducked
//! level as the user's own).
//!
//! The worker drives macOS through `osascript` Standard Additions (`get volume
//! settings`, `set volume output volume`). The crate denies `unsafe_code`,
//! which rules out raw `CoreAudio` property calls, no crate already in the tree
//! exposes a safe volume API, and the accessibility daemon is kept free for
//! the press-time context read it serves.

use std::collections::VecDeque;
use std::process::Command;
use std::sync::OnceLock;
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::Duration;

use tracing::{info, warn};

/// The ducked level as a percentage of the user's volume: low enough that
/// music stops competing with the speaker, high enough to keep a call or a
/// video audible.
const DUCK_PERCENT: u16 = 30;
/// At or below this output volume (0-100) the audio is already quiet and
/// Bolo leaves it alone.
const MIN_DUCKABLE_VOLUME: u8 = 10;
/// Output devices quantize their volume steps, so the level read back right
/// after a set can sit one step away from the level requested. The ducked
/// level is the read-back value, and the restore check allows this slack.
const VOLUME_TOLERANCE: u8 = 1;
/// How long quitting waits for the worker to put the volume back.
const EXIT_RESTORE_TIMEOUT: Duration = Duration::from_millis(1_500);

/// Returns the output volume and mute state in one `osascript` call.
const READ_VOLUME_SCRIPT: [&str; 2] = [
    "set bolo_settings to get volume settings",
    "return ((output volume of bolo_settings) as text) & \",\" & ((output muted of bolo_settings) as text)",
];

/// The system output state as `get volume settings` reports it.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct VolumeSettings {
    /// Output volume, 0-100.
    pub(crate) volume: u8,
    /// Whether output is muted.
    pub(crate) muted: bool,
}

/// What a duck changed, kept until the matching restore.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Ducked {
    /// The user's volume before the duck, which the restore puts back.
    original: u8,
    /// The level actually in effect after the duck (read back from the
    /// device), which tells the restore whether the user moved it since.
    applied: u8,
}

/// Parse the `"<volume>,<muted>"` line `READ_VOLUME_SCRIPT` prints. Devices
/// without a software volume (some HDMI and digital outputs) answer
/// `missing value`, which parses to `None` so the caller skips them.
pub(crate) fn parse_volume_settings(output: &str) -> Option<VolumeSettings> {
    let (volume, muted) = output.trim().split_once(',')?;
    let volume = volume
        .trim()
        .parse::<u8>()
        .ok()
        .filter(|volume| *volume <= 100)?;
    let muted = match muted.trim() {
        "true" => true,
        "false" => false,
        _ => return None,
    };
    Some(VolumeSettings { volume, muted })
}

/// The level to duck to, or `None` when ducking would do nothing useful:
/// output is muted or the volume is already at or below the quiet floor.
pub(crate) fn duck_target(settings: VolumeSettings) -> Option<u8> {
    if settings.muted || settings.volume <= MIN_DUCKABLE_VOLUME {
        return None;
    }
    let target = (u16::from(settings.volume) * DUCK_PERCENT + 50) / 100;
    u8::try_from(target)
        .ok()
        .filter(|target| *target < settings.volume)
}

/// Whether release may put the original volume back. Only when the output
/// still sits at the level Bolo set: a user who changed the volume or muted
/// during the recording keeps their choice.
const fn should_restore(ducked: Ducked, current: VolumeSettings) -> bool {
    !current.muted && current.volume.abs_diff(ducked.applied) <= VOLUME_TOLERANCE
}

/// System volume access, split out so the duck and restore rules are
/// testable without touching the real output device.
trait VolumeControl {
    /// Current output state, or `None` when it cannot be read.
    fn read(&mut self) -> Option<VolumeSettings>;
    /// Set the output volume and return the state read back afterwards.
    fn set(&mut self, volume: u8) -> Option<VolumeSettings>;
}

/// The real output device, through `osascript`.
#[derive(Clone, Copy, Debug)]
struct OsascriptVolume;

impl OsascriptVolume {
    fn run(lines: &[&str]) -> Option<VolumeSettings> {
        let output = match Command::new("osascript")
            .args(lines.iter().flat_map(|line| ["-e", *line]))
            .output()
        {
            Ok(output) => output,
            Err(error) => {
                warn!("[quiet_audio] osascript failed to run: {error}");
                return None;
            }
        };
        if !output.status.success() {
            warn!("[quiet_audio] osascript exited with {}", output.status);
            return None;
        }
        parse_volume_settings(&String::from_utf8_lossy(&output.stdout))
    }
}

impl VolumeControl for OsascriptVolume {
    fn read(&mut self) -> Option<VolumeSettings> {
        Self::run(&READ_VOLUME_SCRIPT)
    }

    fn set(&mut self, volume: u8) -> Option<VolumeSettings> {
        let set_line = format!("set volume output volume {volume}");
        Self::run(&[
            set_line.as_str(),
            READ_VOLUME_SCRIPT[0],
            READ_VOLUME_SCRIPT[1],
        ])
    }
}

/// One instruction for the worker thread.
#[derive(Debug)]
enum Request {
    /// A recording started: lower the volume.
    Duck,
    /// The recording stopped: put the volume back.
    Restore,
    /// The app is exiting: put the volume back, then acknowledge.
    Flush(Sender<()>),
}

/// The worker's state: the volume device plus the duck awaiting a restore.
#[derive(Debug)]
struct Worker<V> {
    control: V,
    ducked: Option<Ducked>,
}

impl<V: VolumeControl> Worker<V> {
    const fn new(control: V) -> Self {
        Self {
            control,
            ducked: None,
        }
    }

    fn duck(&mut self) {
        if self.ducked.is_some() {
            // The previous duck was never restored; keep its original so the
            // eventual restore still returns to the user's real level.
            return;
        }
        let Some(settings) = self.control.read() else {
            info!("[quiet_audio] skipped reason=volume_unavailable");
            return;
        };
        let Some(target) = duck_target(settings) else {
            let reason = if settings.muted {
                "muted"
            } else {
                "already_low"
            };
            info!("[quiet_audio] skipped reason={reason}");
            return;
        };
        // A failed read-back still set the level on most devices, so assume
        // the request took effect; the restore check tolerates one step.
        let applied = self
            .control
            .set(target)
            .map_or(target, |after| after.volume);
        self.ducked = Some(Ducked {
            original: settings.volume,
            applied,
        });
        info!(
            "[quiet_audio] ducked {}",
            serde_json::json!({"from": settings.volume, "to": applied})
        );
    }

    fn restore(&mut self) {
        let Some(ducked) = self.ducked.take() else {
            return;
        };
        let Some(current) = self.control.read() else {
            info!("[quiet_audio] restore skipped reason=volume_unavailable");
            return;
        };
        if !should_restore(ducked, current) {
            info!(
                "[quiet_audio] restore skipped {}",
                serde_json::json!({
                    "reason": if current.muted { "muted_by_user" } else { "changed_by_user" },
                    "current": current.volume,
                })
            );
            return;
        }
        if let Some(after) = self.control.set(ducked.original) {
            info!(
                "[quiet_audio] restored {}",
                serde_json::json!({"to": after.volume})
            );
        } else {
            warn!("[quiet_audio] restore could not confirm the new volume");
        }
    }
}

/// Apply requests in arrival order until every sender is gone.
fn run_worker<V: VolumeControl>(receiver: &Receiver<Request>, worker: &mut Worker<V>) {
    let mut backlog = VecDeque::new();
    loop {
        let request = match backlog.pop_front() {
            Some(request) => request,
            None => match receiver.recv() {
                Ok(request) => request,
                Err(_) => return,
            },
        };
        match request {
            Request::Duck => {
                // A release already queued behind this press (a tap shorter
                // than one osascript round trip) cancels both: ducking now
                // would only blip the user's audio down and back up.
                backlog.extend(receiver.try_iter());
                if matches!(backlog.front(), Some(Request::Restore)) {
                    let _restore = backlog.pop_front();
                    info!("[quiet_audio] skipped reason=released_before_duck");
                    continue;
                }
                worker.duck();
            }
            Request::Restore => worker.restore(),
            Request::Flush(ack) => {
                worker.restore();
                let _sent = ack.send(());
            }
        }
    }
}

static WORKER: OnceLock<Sender<Request>> = OnceLock::new();

/// Start the volume worker when the feature is on. Called once at startup,
/// so the press path never pays for a thread spawn or an env-file read.
pub(crate) fn start(enabled: bool) {
    if !enabled {
        return;
    }
    let (sender, receiver) = mpsc::channel();
    let spawned = std::thread::Builder::new()
        .name(String::from("bolo-quiet-audio"))
        .spawn(move || run_worker(&receiver, &mut Worker::new(OsascriptVolume)));
    match spawned {
        Ok(handle) => {
            drop(handle);
            if WORKER.set(sender).is_err() {
                warn!("[quiet_audio] worker already running; ignoring a second start");
                return;
            }
            info!("[quiet_audio] enabled: other audio is lowered while dictating");
        }
        Err(error) => warn!("[quiet_audio] worker failed to start: {error}"),
    }
}

/// Held by an active recording: lowers other audio when created and asks for
/// the restore when dropped, so every way a recording ends (release, a
/// too-short tap, an error return, the watchdog) puts the volume back.
#[derive(Debug)]
pub(crate) struct QuietAudioGuard {
    sender: Sender<Request>,
}

impl QuietAudioGuard {
    /// Queue a duck. `None` when the feature is off or the worker is gone,
    /// in which case dictation proceeds exactly as without the feature.
    pub(crate) fn engage() -> Option<Self> {
        let sender = WORKER.get()?.clone();
        sender.send(Request::Duck).ok()?;
        Some(Self { sender })
    }
}

impl Drop for QuietAudioGuard {
    fn drop(&mut self) {
        if self.sender.send(Request::Restore).is_err() {
            warn!("[quiet_audio] worker gone; volume not restored");
        }
    }
}

/// Put the volume back before the process exits. Exits skip destructors, so
/// a recording still in flight would otherwise leave the output ducked. A
/// no-op when the feature is off; bounded so quitting never hangs.
pub(crate) fn restore_before_exit() {
    let Some(sender) = WORKER.get() else {
        return;
    };
    let (ack, done) = mpsc::channel();
    if sender.send(Request::Flush(ack)).is_err() {
        return;
    }
    if done.recv_timeout(EXIT_RESTORE_TIMEOUT).is_err() {
        warn!("[quiet_audio] restore before exit timed out");
    }
}

#[cfg(test)]
mod tests {
    use super::{
        Ducked, Request, VolumeControl, VolumeSettings, Worker, duck_target, parse_volume_settings,
        run_worker, should_restore,
    };
    use std::sync::mpsc;

    const fn settings(volume: u8, muted: bool) -> VolumeSettings {
        VolumeSettings { volume, muted }
    }

    /// An output device in memory. `quantize` rounds every set down to an
    /// even step, like hardware with coarse volume steps.
    #[derive(Debug, Default)]
    struct FakeVolume {
        current: Option<VolumeSettings>,
        quantize: bool,
        sets: Vec<u8>,
    }

    impl VolumeControl for FakeVolume {
        fn read(&mut self) -> Option<VolumeSettings> {
            self.current
        }

        fn set(&mut self, volume: u8) -> Option<VolumeSettings> {
            self.sets.push(volume);
            let applied = if self.quantize {
                volume / 2 * 2
            } else {
                volume
            };
            let current = self.current.as_mut()?;
            current.volume = applied;
            Some(*current)
        }
    }

    fn worker_at(volume: u8, muted: bool) -> Worker<FakeVolume> {
        Worker::new(FakeVolume {
            current: Some(settings(volume, muted)),
            ..FakeVolume::default()
        })
    }

    #[test]
    fn parses_the_volume_settings_line() {
        assert_eq!(
            parse_volume_settings("45,false\n"),
            Some(settings(45, false))
        );
        assert_eq!(parse_volume_settings("0,true"), Some(settings(0, true)));
        assert_eq!(
            parse_volume_settings(" 100 , false "),
            Some(settings(100, false))
        );
    }

    #[test]
    fn rejects_devices_without_a_software_volume_and_garbage() {
        assert_eq!(parse_volume_settings("missing value,missing value"), None);
        assert_eq!(parse_volume_settings("45"), None);
        assert_eq!(parse_volume_settings("101,false"), None);
        assert_eq!(parse_volume_settings("-3,false"), None);
        assert_eq!(parse_volume_settings("45,maybe"), None);
        assert_eq!(parse_volume_settings(""), None);
    }

    #[test]
    fn duck_target_is_thirty_percent_of_the_user_volume() {
        assert_eq!(duck_target(settings(100, false)), Some(30));
        assert_eq!(duck_target(settings(50, false)), Some(15));
        assert_eq!(duck_target(settings(45, false)), Some(14));
        assert_eq!(duck_target(settings(11, false)), Some(3));
    }

    #[test]
    fn muted_or_already_quiet_output_is_left_alone() {
        assert_eq!(duck_target(settings(80, true)), None);
        assert_eq!(duck_target(settings(10, false)), None);
        assert_eq!(duck_target(settings(3, false)), None);
        assert_eq!(duck_target(settings(0, false)), None);
    }

    #[test]
    fn restore_only_when_the_volume_still_sits_where_bolo_left_it() {
        let ducked = Ducked {
            original: 60,
            applied: 18,
        };
        assert!(should_restore(ducked, settings(18, false)));
        // One quantization step either way still counts as untouched.
        assert!(should_restore(ducked, settings(17, false)));
        assert!(should_restore(ducked, settings(19, false)));
        // The user moved it, or muted: their choice stands.
        assert!(!should_restore(ducked, settings(40, false)));
        assert!(!should_restore(ducked, settings(5, false)));
        assert!(!should_restore(ducked, settings(18, true)));
    }

    #[test]
    fn duck_then_restore_returns_to_the_original_volume() {
        let mut worker = worker_at(60, false);
        worker.duck();
        assert_eq!(worker.control.current, Some(settings(18, false)));
        worker.restore();
        assert_eq!(worker.control.current, Some(settings(60, false)));
        assert_eq!(worker.control.sets, vec![18, 60]);
    }

    #[test]
    fn a_volume_change_during_recording_is_not_stomped() {
        let mut worker = worker_at(60, false);
        worker.duck();
        worker.control.current = Some(settings(35, false));
        worker.restore();
        assert_eq!(worker.control.current, Some(settings(35, false)));
        assert_eq!(worker.control.sets, vec![18]);
    }

    #[test]
    fn a_mute_during_recording_is_not_stomped() {
        let mut worker = worker_at(60, false);
        worker.duck();
        worker.control.current = Some(settings(18, true));
        worker.restore();
        assert_eq!(worker.control.current, Some(settings(18, true)));
        assert_eq!(worker.control.sets, vec![18]);
    }

    #[test]
    fn quantized_devices_still_restore() {
        let mut worker = worker_at(50, false);
        worker.control.quantize = true;
        worker.duck();
        // 15 requested, the device settled on 14: the read-back level is
        // what the restore compares against.
        assert_eq!(worker.ducked.map(|ducked| ducked.applied), Some(14));
        worker.restore();
        assert_eq!(worker.control.sets, vec![15, 50]);
        assert_eq!(worker.control.current, Some(settings(50, false)));
    }

    #[test]
    fn muted_low_or_unreadable_output_is_never_touched() {
        for mut worker in [worker_at(70, true), worker_at(8, false)] {
            worker.duck();
            worker.restore();
            assert!(worker.control.sets.is_empty());
        }
        let mut worker = Worker::new(FakeVolume::default());
        worker.duck();
        worker.restore();
        assert!(worker.control.sets.is_empty());
    }

    #[test]
    fn an_output_device_that_vanishes_before_release_is_left_alone() {
        let mut worker = worker_at(60, false);
        worker.duck();
        worker.control.current = None;
        worker.restore();
        assert_eq!(worker.control.sets, vec![18]);
        assert_eq!(worker.ducked, None);
    }

    #[test]
    fn a_tap_released_before_the_duck_ran_changes_nothing() {
        let (sender, receiver) = mpsc::channel();
        for request in [Request::Duck, Request::Restore] {
            assert!(sender.send(request).is_ok());
        }
        drop(sender);
        let mut worker = worker_at(60, false);
        run_worker(&receiver, &mut worker);
        assert!(worker.control.sets.is_empty());
    }

    #[test]
    fn exit_flush_restores_a_recording_still_in_flight() {
        let (sender, receiver) = mpsc::channel();
        let (ack, done) = mpsc::channel();
        assert!(sender.send(Request::Duck).is_ok());
        assert!(sender.send(Request::Flush(ack)).is_ok());
        // The guard's own restore after the flush finds nothing to undo.
        assert!(sender.send(Request::Restore).is_ok());
        drop(sender);
        let mut worker = worker_at(60, false);
        run_worker(&receiver, &mut worker);
        assert_eq!(worker.control.sets, vec![18, 60]);
        assert!(done.try_recv().is_ok());
    }
}

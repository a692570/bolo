//! Picks up an Accessibility grant while Bolo keeps running.
//!
//! Bolo ships ad-hoc signed, so every new build has a new code-signing hash
//! and macOS stops honoring the previous build's Accessibility grant. After
//! the user grants it again, the long-lived accessibility daemon keeps
//! answering "untrusted" (observed 2026-11-03: the daemon read untrusted for
//! an hour while a freshly started helper read trusted at once), so only a
//! process started after the grant can see it.
//!
//! This module holds the parts main.rs wires together, kept free of process
//! spawning so tests can drive them with fakes:
//! * [`TrustWatch`]: armed while Bolo is known to be untrusted. The watcher
//!   thread parks on it otherwise, so a trusted Bolo runs no timer at all.
//! * [`watch_until_trusted`]: the fixed-cadence poll, one fresh check per
//!   tick, ending on the first trusted reading.
//! * [`FreshCheckGate`]: serializes fresh checks between the watcher and the
//!   paste path, so at most one check process exists at a time.
//! * [`recheck_with`]: the paste-time second opinion, free while unarmed.
//! * [`fix_permission_plan`] and [`run_fix_permission`]: the menu's Fix
//!   Accessibility Permission action. It clears a stale entry from an older
//!   build (shown as on, honored for nothing) by resetting Bolo's own
//!   Accessibility record, then opens the pane for a clean re-add.

use std::sync::{Condvar, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use super::AccessibilityTrust;

/// How often an untrusted Bolo rechecks with a fresh helper process.
pub(crate) const TRUST_RECHECK_INTERVAL: Duration = Duration::from_secs(2);

/// Whether Bolo is waiting for an Accessibility grant.
#[derive(Debug)]
pub(crate) struct TrustWatch {
    armed: Mutex<bool>,
    wake: Condvar,
}

impl TrustWatch {
    pub(crate) const fn new() -> Self {
        Self {
            armed: Mutex::new(false),
            wake: Condvar::new(),
        }
    }

    fn armed_flag(&self) -> MutexGuard<'_, bool> {
        self.armed.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Start waiting for a grant. True only when this call armed an idle
    /// watch; arming an armed watch changes nothing.
    pub(crate) fn arm(&self) -> bool {
        let mut armed = self.armed_flag();
        if *armed {
            return false;
        }
        *armed = true;
        drop(armed);
        self.wake.notify_all();
        true
    }

    /// Stop waiting. True only for the one caller that disarmed an armed
    /// watch, so recovery and its notification run once per grant even
    /// when the watcher and the paste path see the grant together.
    pub(crate) fn disarm(&self) -> bool {
        std::mem::replace(&mut *self.armed_flag(), false)
    }

    pub(crate) fn is_armed(&self) -> bool {
        *self.armed_flag()
    }

    /// Park the calling thread until the watch is armed.
    pub(crate) fn wait_until_armed(&self) {
        let armed = self
            .wake
            .wait_while(self.armed_flag(), |armed| !*armed)
            .unwrap_or_else(PoisonError::into_inner);
        drop(armed);
    }
}

/// How one armed watch ended.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum WatchOutcome {
    /// A fresh check read trusted and this watch disarmed: run recovery.
    Granted,
    /// Another caller disarmed first (the paste-time recheck saw the grant
    /// and already ran recovery).
    Disarmed,
    /// The check itself failed (helper missing or crashing). The watch stops
    /// so a broken helper is not respawned every tick; the next untrusted
    /// reading anywhere arms it again.
    CheckUnavailable,
}

/// Poll `check` every `interval` while `watch` stays armed. Each tick runs
/// exactly one check and waits for it, so checks never overlap. `pause` is
/// the sleep, injected so tests run without waiting.
pub(crate) fn watch_until_trusted(
    watch: &TrustWatch,
    interval: Duration,
    mut check: impl FnMut() -> AccessibilityTrust,
    mut pause: impl FnMut(Duration),
) -> WatchOutcome {
    loop {
        pause(interval);
        if !watch.is_armed() {
            return WatchOutcome::Disarmed;
        }
        match check() {
            AccessibilityTrust::Trusted => {
                return if watch.disarm() {
                    WatchOutcome::Granted
                } else {
                    WatchOutcome::Disarmed
                };
            }
            AccessibilityTrust::Untrusted => {}
            AccessibilityTrust::Unavailable => {
                let _was_armed = watch.disarm();
                return WatchOutcome::CheckUnavailable;
            }
        }
    }
}

/// Serializes fresh trust checks: holding the gate while a check process
/// runs means the watcher and the paste path never run two at once.
#[derive(Debug)]
pub(crate) struct FreshCheckGate {
    in_flight: Mutex<()>,
}

impl FreshCheckGate {
    pub(crate) const fn new() -> Self {
        Self {
            in_flight: Mutex::new(()),
        }
    }

    /// Run `check` once no other check is in flight.
    pub(crate) fn run<T>(&self, check: impl FnOnce() -> T) -> T {
        let _in_flight = self
            .in_flight
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        check()
    }
}

/// Result of one paste-time trust recheck.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum RecheckOutcome {
    /// Bolo is not waiting for a grant: nothing ran.
    NotWatching,
    /// The fresh check still reads untrusted (or failed).
    StillUntrusted,
    /// The fresh check read trusted. `run_recovery` is true for the one
    /// caller that disarmed the watch and must restart the helpers; false
    /// means the watcher disarmed between this check and the disarm and is
    /// running recovery itself.
    Granted { run_recovery: bool },
}

/// One fresh check, only while `watch` is armed. An unarmed watch, which is
/// every session that was trusted at startup, costs a mutex read.
pub(crate) fn recheck_with(
    watch: &TrustWatch,
    fresh_check: impl FnOnce() -> AccessibilityTrust,
) -> RecheckOutcome {
    if !watch.is_armed() {
        return RecheckOutcome::NotWatching;
    }
    match fresh_check() {
        AccessibilityTrust::Trusted => RecheckOutcome::Granted {
            run_recovery: watch.disarm(),
        },
        AccessibilityTrust::Untrusted | AccessibilityTrust::Unavailable => {
            RecheckOutcome::StillUntrusted
        }
    }
}

/// Bolo's bundle id, as written to Info.plist by `scripts/build-dmg.sh`.
pub(crate) const BOLO_BUNDLE_ID: &str = "com.a692570.bolo";

/// Resets Bolo's own Accessibility record and nothing else: never another
/// app, never every app, never another permission.
pub(crate) const RESET_COMMAND: (&str, [&str; 3]) = (
    "/usr/bin/tccutil",
    ["reset", "Accessibility", BOLO_BUNDLE_ID],
);

/// Opens System Settings on the Accessibility list (the same deep link the
/// setup window uses).
pub(crate) const OPEN_SETTINGS_COMMAND: (&str, [&str; 1]) = (
    "/usr/bin/open",
    ["x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
);

/// What the Fix Accessibility Permission action does for one trust reading.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum FixPermissionPlan {
    /// Already trusted: a reset would revoke a working grant, so nothing runs.
    AlreadyTrusted,
    /// Bundle mode: reset Bolo's record, then open the pane.
    ResetAndOpen,
    /// Source checkout: the grant belongs to the Python interpreter, not to
    /// the bundle id, so only the pane opens.
    OpenOnly,
    /// The trust helper itself fails; a permission reset cannot fix that.
    HelperBroken,
}

/// The plan for a fresh trust reading. Only an untrusted reading resets.
pub(crate) const fn fix_permission_plan(
    bundle: bool,
    trust: AccessibilityTrust,
) -> FixPermissionPlan {
    match trust {
        AccessibilityTrust::Trusted => FixPermissionPlan::AlreadyTrusted,
        AccessibilityTrust::Unavailable => FixPermissionPlan::HelperBroken,
        AccessibilityTrust::Untrusted if bundle => FixPermissionPlan::ResetAndOpen,
        AccessibilityTrust::Untrusted => FixPermissionPlan::OpenOnly,
    }
}

/// What actually ran: `None` for a step the plan skips, else whether the
/// command succeeded.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct FixPermissionOutcome {
    pub(crate) plan: FixPermissionPlan,
    pub(crate) reset: Option<bool>,
    pub(crate) opened: Option<bool>,
}

/// Run the plan's commands through `run(program, args)`, which reports
/// success. The pane opens even when the reset fails, because the user can
/// still remove the stale entry by hand.
pub(crate) fn run_fix_permission(
    plan: FixPermissionPlan,
    mut run: impl FnMut(&str, &[&str]) -> bool,
) -> FixPermissionOutcome {
    let reset = matches!(plan, FixPermissionPlan::ResetAndOpen)
        .then(|| run(RESET_COMMAND.0, &RESET_COMMAND.1));
    let opened = matches!(
        plan,
        FixPermissionPlan::ResetAndOpen | FixPermissionPlan::OpenOnly
    )
    .then(|| run(OPEN_SETTINGS_COMMAND.0, &OPEN_SETTINGS_COMMAND.1));
    FixPermissionOutcome {
        plan,
        reset,
        opened,
    }
}

/// Second guidance line after the action: the watcher takes it from here.
const FIX_GUIDANCE_TAIL: &str = "Bolo will start pasting by itself.";

/// Notification (title, two-line body) telling the user what to do next.
pub(crate) fn fix_permission_notice(
    outcome: FixPermissionOutcome,
    bundle: bool,
    python: &str,
) -> (&'static str, String) {
    let open_hint = if outcome.opened == Some(false) {
        "Open System Settings > Privacy & Security > Accessibility. "
    } else {
        ""
    };
    match outcome.plan {
        FixPermissionPlan::AlreadyTrusted => (
            "Bolo can already paste",
            String::from("Accessibility is on for Bolo, so nothing was reset."),
        ),
        FixPermissionPlan::HelperBroken => (
            "Bolo helper needs repair",
            String::from(if bundle {
                "Reinstall Bolo by downloading the latest Bolo DMG again."
            } else {
                "Run ./install.sh, then ./restart.sh to repair Bolo's Python helper."
            }),
        ),
        FixPermissionPlan::ResetAndOpen if outcome.reset == Some(true) => (
            "Turn on Bolo in Accessibility",
            format!(
                "{open_hint}Add Bolo with the plus button and turn it on.\n{FIX_GUIDANCE_TAIL}"
            ),
        ),
        FixPermissionPlan::ResetAndOpen => (
            "Turn on Bolo in Accessibility",
            format!(
                "{open_hint}Remove Bolo with the minus button, add it again with the plus \
                 button and turn it on.\n{FIX_GUIDANCE_TAIL}"
            ),
        ),
        FixPermissionPlan::OpenOnly => (
            "Turn on Bolo's Python in Accessibility",
            format!(
                "{open_hint}Add this Python interpreter with the plus button and turn it on: \
                 {python}\n{FIX_GUIDANCE_TAIL}"
            ),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::{
        AccessibilityTrust, BOLO_BUNDLE_ID, FixPermissionOutcome, FixPermissionPlan,
        FreshCheckGate, OPEN_SETTINGS_COMMAND, RESET_COMMAND, RecheckOutcome,
        TRUST_RECHECK_INTERVAL, TrustWatch, WatchOutcome, fix_permission_notice,
        fix_permission_plan, recheck_with, run_fix_permission, watch_until_trusted,
    };
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    #[test]
    fn arm_and_disarm_each_report_only_the_first_transition() {
        let watch = TrustWatch::new();
        assert!(!watch.is_armed());
        assert!(watch.arm());
        assert!(!watch.arm(), "re-arming an armed watch is a no-op");
        assert!(watch.is_armed());
        assert!(watch.disarm());
        assert!(!watch.disarm(), "only one caller may run recovery");
        assert!(!watch.is_armed());
    }

    #[test]
    fn watch_polls_on_the_fixed_cadence_until_the_grant_then_stops() {
        let watch = TrustWatch::new();
        let _armed = watch.arm();
        let mut readings = vec![
            AccessibilityTrust::Trusted,
            AccessibilityTrust::Untrusted,
            AccessibilityTrust::Untrusted,
            AccessibilityTrust::Untrusted,
        ];
        let mut checks = 0;
        let mut pauses = Vec::new();
        let outcome = watch_until_trusted(
            &watch,
            TRUST_RECHECK_INTERVAL,
            || {
                checks += 1;
                readings.pop().unwrap_or(AccessibilityTrust::Untrusted)
            },
            |interval| pauses.push(interval),
        );
        assert_eq!(outcome, WatchOutcome::Granted);
        assert_eq!(checks, 4);
        assert_eq!(pauses, vec![Duration::from_secs(2); 4]);
        assert!(!watch.is_armed(), "a granted watch stops polling");
    }

    #[test]
    fn watch_stops_without_checking_once_another_caller_disarmed() {
        let watch = TrustWatch::new();
        let _armed = watch.arm();
        let mut checks = 0;
        let outcome = watch_until_trusted(
            &watch,
            Duration::ZERO,
            || {
                checks += 1;
                AccessibilityTrust::Untrusted
            },
            |_| {
                // The paste path recovers during this pause.
                let _disarmed = watch.disarm();
            },
        );
        assert_eq!(outcome, WatchOutcome::Disarmed);
        assert_eq!(checks, 0);
    }

    #[test]
    fn watch_gives_up_on_a_broken_helper_instead_of_respawning_it() {
        let watch = TrustWatch::new();
        let _armed = watch.arm();
        let mut checks = 0;
        let outcome = watch_until_trusted(
            &watch,
            Duration::ZERO,
            || {
                checks += 1;
                AccessibilityTrust::Unavailable
            },
            |_| {},
        );
        assert_eq!(outcome, WatchOutcome::CheckUnavailable);
        assert_eq!(checks, 1);
        assert!(!watch.is_armed());
        assert!(watch.arm(), "a later untrusted reading can arm it again");
    }

    #[test]
    fn wait_until_armed_parks_until_an_arm_arrives() {
        let watch = Arc::new(TrustWatch::new());
        let waiter_watch = Arc::clone(&watch);
        let waiter = std::thread::spawn(move || waiter_watch.wait_until_armed());
        std::thread::sleep(Duration::from_millis(50));
        assert!(
            !waiter.is_finished(),
            "an idle watch keeps the watcher parked"
        );
        assert!(watch.arm());
        assert!(waiter.join().is_ok());
    }

    #[test]
    fn fresh_checks_never_overlap() {
        let gate = Arc::new(FreshCheckGate::new());
        let running = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));
        let workers: Vec<_> = (0..8)
            .map(|_| {
                let gate = Arc::clone(&gate);
                let running = Arc::clone(&running);
                let peak = Arc::clone(&peak);
                std::thread::spawn(move || {
                    gate.run(|| {
                        let now = running.fetch_add(1, Ordering::SeqCst) + 1;
                        let _previous_peak = peak.fetch_max(now, Ordering::SeqCst);
                        std::thread::sleep(Duration::from_millis(5));
                        let _previous_running = running.fetch_sub(1, Ordering::SeqCst);
                    });
                })
            })
            .collect();
        for worker in workers {
            assert!(worker.join().is_ok());
        }
        assert_eq!(peak.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn recheck_runs_nothing_while_the_watch_is_idle() {
        let watch = TrustWatch::new();
        let mut checks = 0;
        let outcome = recheck_with(&watch, || {
            checks += 1;
            AccessibilityTrust::Trusted
        });
        assert_eq!(outcome, RecheckOutcome::NotWatching);
        assert_eq!(checks, 0, "a trusted session pays no paste-time check");
    }

    #[test]
    fn recheck_reports_the_grant_and_hands_recovery_to_one_caller() {
        let watch = TrustWatch::new();
        let _armed = watch.arm();
        assert_eq!(
            recheck_with(&watch, || AccessibilityTrust::Untrusted),
            RecheckOutcome::StillUntrusted
        );
        assert!(watch.is_armed(), "still waiting after an untrusted recheck");
        assert_eq!(
            recheck_with(&watch, || AccessibilityTrust::Unavailable),
            RecheckOutcome::StillUntrusted
        );
        assert_eq!(
            recheck_with(&watch, || AccessibilityTrust::Trusted),
            RecheckOutcome::Granted { run_recovery: true }
        );
        assert!(!watch.is_armed());
        // The watcher saw the grant first and already recovered: the watch
        // is idle again, so the paste path runs no check of its own.
        assert!(watch.arm());
        assert!(watch.disarm(), "the watcher wins this grant");
        assert_eq!(
            recheck_with(&watch, || AccessibilityTrust::Trusted),
            RecheckOutcome::NotWatching
        );
    }

    #[test]
    fn reset_command_names_only_bolos_accessibility_record() {
        assert_eq!(RESET_COMMAND.0, "/usr/bin/tccutil");
        assert_eq!(
            RESET_COMMAND.1,
            ["reset", "Accessibility", "com.a692570.bolo"]
        );
        assert!(!RESET_COMMAND.1.contains(&"All"));
        assert_eq!(OPEN_SETTINGS_COMMAND.0, "/usr/bin/open");
        assert_eq!(
            OPEN_SETTINGS_COMMAND.1,
            ["x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"]
        );
    }

    #[test]
    fn bundle_id_matches_the_info_plist_the_build_writes() {
        let build_script = include_str!("../scripts/build-dmg.sh");
        let key = "<key>CFBundleIdentifier</key>";
        let after_key = build_script
            .split_once(key)
            .map(|(_, rest)| rest.trim_start())
            .unwrap_or_default();
        let expected = format!("<string>{BOLO_BUNDLE_ID}</string>");
        assert!(
            after_key.starts_with(&expected),
            "Info.plist bundle id drifted from BOLO_BUNDLE_ID"
        );
    }

    #[test]
    fn only_an_untrusted_reading_resets_and_only_in_bundle_mode() {
        for bundle in [true, false] {
            assert_eq!(
                fix_permission_plan(bundle, AccessibilityTrust::Trusted),
                FixPermissionPlan::AlreadyTrusted
            );
            assert_eq!(
                fix_permission_plan(bundle, AccessibilityTrust::Unavailable),
                FixPermissionPlan::HelperBroken
            );
        }
        assert_eq!(
            fix_permission_plan(true, AccessibilityTrust::Untrusted),
            FixPermissionPlan::ResetAndOpen
        );
        assert_eq!(
            fix_permission_plan(false, AccessibilityTrust::Untrusted),
            FixPermissionPlan::OpenOnly
        );
    }

    fn run_with_fake(
        plan: FixPermissionPlan,
        succeed: bool,
    ) -> (FixPermissionOutcome, Vec<String>) {
        let mut calls = Vec::new();
        let outcome = run_fix_permission(plan, |program, args| {
            calls.push(format!("{program} {}", args.join(" ")));
            succeed
        });
        (outcome, calls)
    }

    #[test]
    fn fix_runs_the_reset_then_opens_the_pane() {
        let (outcome, calls) = run_with_fake(FixPermissionPlan::ResetAndOpen, true);
        assert_eq!(
            calls,
            vec![
                String::from("/usr/bin/tccutil reset Accessibility com.a692570.bolo"),
                String::from(
                    "/usr/bin/open x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
                ),
            ]
        );
        assert_eq!(outcome.reset, Some(true));
        assert_eq!(outcome.opened, Some(true));
        let (title, body) = fix_permission_notice(outcome, true, "/unused/python3");
        assert_eq!(title, "Turn on Bolo in Accessibility");
        assert_eq!(
            body,
            "Add Bolo with the plus button and turn it on.\nBolo will start pasting by itself."
        );
    }

    #[test]
    fn a_failed_reset_still_opens_the_pane_and_asks_for_the_minus_button() {
        let (outcome, calls) = run_with_fake(FixPermissionPlan::ResetAndOpen, false);
        assert_eq!(calls.len(), 2, "the pane opens even after a failed reset");
        assert_eq!(outcome.reset, Some(false));
        let (_, body) = fix_permission_notice(outcome, true, "/unused/python3");
        assert!(body.starts_with("Open System Settings > Privacy & Security > Accessibility."));
        assert!(body.contains("Remove Bolo with the minus button"));
        assert!(body.ends_with("\nBolo will start pasting by itself."));
    }

    #[test]
    fn trusted_broken_and_source_runs_never_reset() {
        for plan in [
            FixPermissionPlan::AlreadyTrusted,
            FixPermissionPlan::HelperBroken,
        ] {
            let (outcome, calls) = run_with_fake(plan, true);
            assert!(calls.is_empty(), "{plan:?} must not run any command");
            assert_eq!((outcome.reset, outcome.opened), (None, None));
        }
        let (outcome, calls) = run_with_fake(FixPermissionPlan::OpenOnly, true);
        assert_eq!(calls.len(), 1);
        assert!(calls.iter().all(|call| !call.contains("tccutil")));
        assert_eq!(outcome.reset, None);
        let python = "/Users/demo/.bolo/venv/bin/python3";
        let (_, body) = fix_permission_notice(outcome, false, python);
        assert!(body.contains(python));
        assert!(body.ends_with("\nBolo will start pasting by itself."));
    }

    #[test]
    fn notices_for_trusted_and_broken_helpers_say_what_to_do() {
        let trusted = FixPermissionOutcome {
            plan: FixPermissionPlan::AlreadyTrusted,
            reset: None,
            opened: None,
        };
        let (title, body) = fix_permission_notice(trusted, true, "");
        assert_eq!(title, "Bolo can already paste");
        assert!(body.contains("nothing was reset"));
        let broken = FixPermissionOutcome {
            plan: FixPermissionPlan::HelperBroken,
            reset: None,
            opened: None,
        };
        assert!(fix_permission_notice(broken, true, "").1.contains("DMG"));
        assert!(
            fix_permission_notice(broken, false, "")
                .1
                .contains("./install.sh")
        );
    }
}

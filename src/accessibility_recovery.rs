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

#[cfg(test)]
mod tests {
    use super::{
        AccessibilityTrust, FreshCheckGate, RecheckOutcome, TRUST_RECHECK_INTERVAL, TrustWatch,
        WatchOutcome, recheck_with, watch_until_trusted,
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
}

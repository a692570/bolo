//! Pure push-to-talk recording lifecycle state machine.

/// The current recording lifecycle state.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) enum State {
    /// No recording is active.
    #[default]
    Idle,
    /// A recording has started and is waiting for release.
    Recording,
}

/// Input events accepted by the recording state machine.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Event {
    /// The push-to-talk key was pressed.
    Press,
    /// The push-to-talk key was released.
    Release,
    /// The watchdog found a stale recording.
    WatchdogTimeout,
    /// Starting the recorder failed.
    StartFailed,
}

/// Side effect command produced by a transition.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Command {
    /// Start a new recording.
    StartRecording,
    /// Finish the active recording.
    FinishRecording,
    /// No side effect is needed.
    Ignore,
}

/// A pure push-to-talk state machine.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct RecordingFsm {
    state: State,
}

impl RecordingFsm {
    /// Creates an idle state machine.
    #[cfg(test)]
    #[must_use]
    pub(crate) const fn new() -> Self {
        Self { state: State::Idle }
    }

    /// Returns the current state.
    #[cfg(test)]
    #[must_use]
    pub(crate) const fn state(self) -> State {
        self.state
    }

    /// Applies an event and returns the side effect command for the caller.
    pub(crate) fn handle(&mut self, event: Event) -> Command {
        let (state, command) = transition(self.state, event);
        self.state = state;
        command
    }
}

const fn transition(state: State, event: Event) -> (State, Command) {
    match (state, event) {
        (State::Idle, Event::Press) => (State::Recording, Command::StartRecording),
        (State::Recording, Event::Release | Event::WatchdogTimeout) => {
            (State::Idle, Command::FinishRecording)
        }
        (State::Recording, Event::StartFailed) => (State::Idle, Command::Ignore),
        (state, _) => (state, Command::Ignore),
    }
}

#[cfg(test)]
mod tests {
    use super::{Command, Event, RecordingFsm, State};

    #[test]
    fn starts_recording_on_press_from_idle() {
        let mut fsm = RecordingFsm::new();

        let command = fsm.handle(Event::Press);

        assert_eq!(command, Command::StartRecording);
        assert_eq!(fsm.state(), State::Recording);
    }

    #[test]
    fn ignores_duplicate_press_while_recording() {
        let mut fsm = RecordingFsm::new();
        assert_eq!(fsm.handle(Event::Press), Command::StartRecording);

        let command = fsm.handle(Event::Press);

        assert_eq!(command, Command::Ignore);
        assert_eq!(fsm.state(), State::Recording);
    }

    #[test]
    fn finishes_recording_on_release() {
        let mut fsm = RecordingFsm::new();
        assert_eq!(fsm.handle(Event::Press), Command::StartRecording);

        let command = fsm.handle(Event::Release);

        assert_eq!(command, Command::FinishRecording);
        assert_eq!(fsm.state(), State::Idle);
    }

    #[test]
    fn watchdog_timeout_finishes_recording() {
        let mut fsm = RecordingFsm::new();
        assert_eq!(fsm.handle(Event::Press), Command::StartRecording);

        let command = fsm.handle(Event::WatchdogTimeout);

        assert_eq!(command, Command::FinishRecording);
        assert_eq!(fsm.state(), State::Idle);
    }

    #[test]
    fn ignores_release_without_active_recording() {
        let mut fsm = RecordingFsm::new();

        let command = fsm.handle(Event::Release);

        assert_eq!(command, Command::Ignore);
        assert_eq!(fsm.state(), State::Idle);
    }

    #[test]
    fn start_failed_returns_to_idle() {
        let mut fsm = RecordingFsm::new();
        assert_eq!(fsm.handle(Event::Press), Command::StartRecording);

        assert_eq!(fsm.handle(Event::StartFailed), Command::Ignore);
        assert_eq!(fsm.state(), State::Idle);
    }

    #[test]
    fn release_does_not_block_next_recording() {
        let mut fsm = RecordingFsm::new();
        assert_eq!(fsm.handle(Event::Press), Command::StartRecording);
        assert_eq!(fsm.handle(Event::Release), Command::FinishRecording);
        assert_eq!(fsm.state(), State::Idle);
        assert_eq!(fsm.handle(Event::Press), Command::StartRecording);
        assert_eq!(fsm.state(), State::Recording);
    }
}

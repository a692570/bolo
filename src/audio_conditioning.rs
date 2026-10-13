//! Pre-ASR microphone conditioning through WebRTC's `AudioProcessing`
//! module:
//! `AEC3`, noise suppression, and `AGC1` over 10 ms frames, mirroring the
//! WebRTC conditioning stack Willow Voice runs in front of its ASR.
//!
//! # Why
//! Dictation accuracy quality work identified pre-ASR conditioning as the
//! highest-value improvement over Bolo's raw-capture baseline: noise
//! suppression removes room/hum/fan noise before it reaches the encoder,
//! AGC1 levels the user's voice so the STT sees a consistent operating
//! point, and the high-pass filter the noise suppressor force-enables
//! removes rumble below the speech band.
//!
//! # Honesty about AEC3
//! AEC3 removes loudspeaker echo only when the far-end (render) signal is
//! fed to the processor alongside the capture stream. Dictation has no
//! render stream: Bolo plays no audio while capturing, and macOS gives the
//! process no system-output loopback. The echo canceller is enabled in the
//! config but is never fed a fake far-end reference; with no reference the
//! AEC contributes essentially nothing, and the measurable gain comes from
//! noise suppression plus AGC1. It stays enabled so the configuration
//! matches the standard WebRTC capture chain (and so a future render tap
//! can start cancelling real echo without touching this module).
//!
//! # Downstream contract
//! The conditioner sits between cpal and every audio consumer: the master
//! sample buffer (RMS speech/trailing detection, WAV, batch fallback) and
//! the wire sinks (streaming preview, dictation upload) see only
//! conditioned output. It preserves the pipeline's byte-exact contract:
//! every input sample appears exactly once, in order, in the output
//! stream. Internally it is frame-locked to 10 ms (frame length derives
//! from the sample rate, never hardcoded): arbitrary cpal buffer sizes
//! are buffered into whole frames, so at most one sub-10 ms remainder is
//! in flight at any moment, and `flush` emits that tail unprocessed at
//! end of stream. RMS frames (20 ms) and wire chunks (60 ms) downstream
//! re-buffer independently and are unaffected by the 10 ms alignment.
//!
//! The level inside those buffers changes by design: AGC1 in
//! `AdaptiveDigital` mode (no analog mixer coupling exists to prescribe
//! gains to) targets the WebRTC-default -3 dBFS speech peak, far above
//! the unconditioned dictation level. The RMS thresholds in `main.rs`
//! still seesaw the same way -- speech clears `SPEECH_RMS_THRESHOLD`,
//! rooms sit under it, the trailing floor stays percentile-relative -- so
//! detection semantics survive; only their absolute voltages move.
//! `BOLO_AUDIO_CONDITIONING=off` is the kill switch for the whole stage.

use cpal::Sample as _;
use webrtc_audio_processing::config::{
    EchoCanceller, GainController, GainController1, GainControllerMode, NoiseSuppression,
};
use webrtc_audio_processing::{Config, Processor};

/// Frame-locked wrapper around WebRTC's `Processor` for one capture stream.
///
/// Enforces the two invariants the library would otherwise panic on inside
/// the audio callback: frames handed to `process_capture_frame` are always
/// exactly `sample_rate / 100` samples, and a processing failure degrades
/// to passthrough instead of killing the capture thread. Callers pass the
/// whole mono cpal buffer to [`Self::process`] and hand the returned
/// slice to every downstream consumer.
#[derive(Debug)]
pub(crate) struct AudioConditioner {
    /// The APM handle; its methods take `&self` and the type is `Send` +
    /// `Sync`, but capture frames must be fed strictly in order, so this
    /// is used from the callback (and the end-of-recording flush) under
    /// one owning lock.
    processor: Processor,
    /// 10 ms frame length in samples, taken from the processor itself
    /// (`sample_rate / 100`), never hardcoded.
    frame_len: usize,
    /// Input tail shorter than one frame, awaiting more capture audio.
    pending: Vec<f32>,
    /// Reusable mono channel buffer handed to the processor per frame.
    frame: Vec<f32>,
    /// Completed, fully processed samples for the current process or
    /// flush call.
    ready: Vec<i16>,
    /// Set after a processing failure: later input passes through.
    disabled: bool,
    /// First processing-failure description, surfaced once to the caller
    /// for logging.
    failure: Option<String>,
}

impl AudioConditioner {
    /// Creates a conditioner for a capture stream at `sample_rate`.
    ///
    /// Returns `Err` when the rate cannot form exact 10 ms frames (rates
    /// not divisible by 100) or the underlying APM cannot be built; the
    /// caller falls back to unconditional passthrough and logs.
    pub(crate) fn new(sample_rate: u32) -> Result<Self, String> {
        if sample_rate == 0 {
            return Err(String::from("sample rate must be positive"));
        }
        if !sample_rate.is_multiple_of(100) {
            return Err(format!(
                "sample rate {sample_rate} Hz cannot form exact 10 ms frames"
            ));
        }
        let processor =
            Processor::new(sample_rate).map_err(|error| format!("apm init: {error}"))?;
        processor.set_config(Config {
            // AEC3, delay estimator left adaptive. See the module docs: no
            // render stream is fed, so this stays inert until one exists.
            echo_canceller: Some(EchoCanceller::default()),
            noise_suppression: Some(NoiseSuppression::default()),
            gain_controller: Some(GainController::GainController1(GainController1 {
                // The wrapper exposes no stream-analog-level calls that the
                // AdaptiveAnalog mode would need, and dictation has no
                // coupled analog volume control anyway: gains apply
                // digitally, the mode WebRTC prescribes when no analog
                // control is available.
                mode: GainControllerMode::AdaptiveDigital,
                ..GainController1::default()
            })),
            ..Config::default()
        });
        Ok(Self {
            frame_len: processor.num_samples_per_frame(),
            processor,
            pending: Vec::new(),
            frame: Vec::new(),
            ready: Vec::new(),
            disabled: false,
            failure: None,
        })
    }

    /// The 10 ms frame length in samples, derived from the sample rate
    /// via the processor's own frame-size rule.
    #[must_use]
    pub(crate) const fn frame_len(&self) -> usize {
        self.frame_len
    }

    /// Conditions `input` and returns every sample that completed a whole
    /// 10 ms frame.
    ///
    /// Sub-frame audio stays buffered inside; across the whole stream,
    /// total output equals total input once [`Self::flush`] runs, with no
    /// reordering and no duplication. A processing failure disables
    /// conditioning for the rest of the stream and emits the unprocessed
    /// remainder, so the sample-count contract survives even a
    /// mid-recording APM error.
    pub(crate) fn process(&mut self, input: &[i16]) -> &[i16] {
        self.ready.clear();
        if self.disabled {
            self.ready.extend_from_slice(input);
            return &self.ready;
        }
        self.pending
            .extend(input.iter().copied().map(f32::from_sample));
        while self.pending.len() >= self.frame_len {
            let Self {
                processor,
                frame_len,
                frame,
                pending,
                ready,
                disabled,
                failure,
            } = self;
            frame.clear();
            frame.extend_from_slice(&pending[..*frame_len]);
            if let Err(error) = processor.process_capture_frame(&mut [frame.as_mut_slice()]) {
                // One bad frame must not eat the rest of the recording:
                // hand back everything still held in `pending`, raw.
                ready.extend(pending.iter().copied().map(i16::from_sample));
                pending.clear();
                *disabled = true;
                if failure.is_none() {
                    *failure = Some(error.to_string());
                }
                break;
            }
            ready.extend(frame.iter().copied().map(i16::from_sample));
            drop(pending.drain(..*frame_len));
        }
        &self.ready
    }

    /// Emits the sub-frame remainder that never formed a whole 10 ms
    /// frame, unprocessed, so the stream's total output equals its total
    /// input. Called once at end of recording, after the last capture
    /// callback.
    #[must_use]
    pub(crate) fn flush(&mut self) -> &[i16] {
        self.ready.clear();
        self.ready
            .extend(self.pending.iter().copied().map(i16::from_sample));
        self.pending.clear();
        &self.ready
    }

    /// Takes the first processing-failure description, if any, for the
    /// caller to log once.
    pub(crate) const fn take_failure(&mut self) -> Option<String> {
        self.failure.take()
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::panic_in_result_fn)]
    use super::AudioConditioner;
    use cpal::Sample as _;

    /// Deterministic tone-plus-noise corpus in the i16 domain: a 220 Hz
    /// tone under a pseudo-noise hash, so conditioning has broadband noise
    /// to suppress and a strong tone for AGC1 to act on. A pure function
    /// of the index, so every test builds byte-identical input.
    fn corpus_sample(index: f32) -> i16 {
        // 220 Hz at 48 kHz: phase step 2*pi*220/48000 = 0.028798 rad/sample.
        let tone = (index * 0.028_797_9).sin() * 0.4;
        let hash = (index * 12.989_8).sin() * 43_758.547;
        let noise = hash.fract() - 0.5;
        i16::from_sample((tone + noise * 0.25).clamp(-1.0, 1.0))
    }

    fn corpus(len: usize) -> Vec<i16> {
        let mut samples = Vec::with_capacity(len);
        let mut index = 0.0_f32;
        while samples.len() < len {
            samples.push(corpus_sample(index));
            index += 1.0;
        }
        samples
    }

    /// RMS over the i16 buffer, normalized to exactly the framing the
    /// `main.rs` speech detection uses.
    fn rms(samples: &[i16]) -> f64 {
        let sum_of_squares: f64 = samples
            .iter()
            .map(|&sample| {
                let value = f64::from(sample);
                value * value
            })
            .sum();
        let count = f64::from(u32::try_from(samples.len()).unwrap_or(u32::MAX));
        (sum_of_squares / count.max(1.0) / 32_768.0_f64.powi(2)).sqrt()
    }

    /// Silence must flow through without panicking, in whole and in
    /// parts, and come out silent: `flush` closes the loop so the total
    /// output length equals the input length for arbitrary batch sizes.
    #[test]
    fn silence_passes_through_quiet_and_complete() -> Result<(), String> {
        const RATE: u32 = 16_000;
        const SAMPLES: usize = 17_600;
        let mut conditioner = AudioConditioner::new(RATE)?;

        let mut out: Vec<i16> = Vec::new();
        let mut pushed = 0_usize;
        for size in [0_usize, 1, 159, 160, 161, 479, 512, 1, 960] {
            let end = (pushed + size).min(SAMPLES);
            let input = vec![0_i16; end - pushed];
            out.extend_from_slice(conditioner.process(&input));
            pushed = end;
        }
        out.extend_from_slice(conditioner.process(&vec![0_i16; SAMPLES - pushed]));
        out.extend_from_slice(conditioner.flush());

        assert_eq!(
            out.len(),
            SAMPLES,
            "silence in must equal silence out: no sample lost or duplicated"
        );
        assert!(
            out.iter().copied().all(|sample| sample.abs() <= 32),
            "silence must stay silent through the conditioner (max abs {})",
            out.iter().copied().map(i16::abs).max().unwrap_or_default()
        );
        assert!(
            conditioner.flush().is_empty(),
            "a second flush holds nothing back"
        );
        Ok(())
    }

    /// Conditioning must measurably change the signal: the same loud
    /// tone-over-noise corpus comes out at a different RMS when processed
    /// than the raw input, proving the stage is live and not a passthrough
    /// that silently failed to init.
    #[test]
    fn conditioning_changes_the_level_of_tone_plus_noise() -> Result<(), String> {
        const RATE: u32 = 48_000;
        const SAMPLES: usize = 96_000;
        let input = corpus(SAMPLES);

        let mut conditioner = AudioConditioner::new(RATE)?;
        let mut out: Vec<i16> = Vec::new();
        for frame in input.chunks(480) {
            out.extend_from_slice(conditioner.process(frame));
        }
        out.extend_from_slice(conditioner.flush());
        assert_eq!(out.len(), SAMPLES);

        let raw_rms = rms(&input);
        let conditioned_rms = rms(&out);
        let change = (conditioned_rms - raw_rms).abs() / raw_rms.max(1e-12);
        assert!(
            change > 0.05,
            "conditioning must move the RMS measurably: raw {raw_rms:.6} vs conditioned {conditioned_rms:.6}"
        );
        assert!(
            conditioned_rms > 1e-5,
            "conditioned output must still carry the tone, not just floor"
        );
        Ok(())
    }

    /// The downstream byte-exact contract: output is a pure function of
    /// the sample stream, independent of how cpal split it into push
    /// batches, and after `flush` the stream is complete.
    #[test]
    fn batching_is_invisible_to_the_output() -> Result<(), String> {
        const RATE: u32 = 48_000;
        let input = corpus(2_000);

        // Whole-stream feed.
        let mut whole = AudioConditioner::new(RATE)?;
        let mut whole_out = Vec::new();
        whole_out.extend_from_slice(whole.process(&input));
        whole_out.extend_from_slice(whole.flush());

        // Split feed: uneven, sub-frame, and frame-plus-epsilon batches.
        let mut split = AudioConditioner::new(RATE)?;
        let mut split_out = Vec::new();
        let mut cursor = 0_usize;
        for size in [1_usize, 3, 159, 160, 161, 479, 480, 481, 512, 960] {
            let end = (cursor + size).min(input.len());
            split_out.extend_from_slice(split.process(&input[cursor..end]));
            cursor = end;
        }
        split_out.extend_from_slice(split.process(&input[cursor..]));
        split_out.extend_from_slice(split.flush());

        assert_eq!(whole_out.len(), input.len());
        assert_eq!(
            split_out, whole_out,
            "batch boundaries must not change a single output sample"
        );
        // Frame-aligned batches come back at exactly their input length:
        // the chunk alignment the wire chunkers consume.
        let mut aligned = AudioConditioner::new(RATE)?;
        assert_eq!(aligned.process(&input[..480]).len(), 480);
        assert_eq!(
            aligned.process(&input[480..960]).len(),
            480,
            "the next aligned batch still emits exactly frame multiples"
        );
        assert!(aligned.flush().is_empty());
        Ok(())
    }

    /// The degraded path keeps the pipeline alive: once conditioning is
    /// off, pushes pass through raw and complete, with no loss, and no
    /// stale failure is reported that never happened.
    #[test]
    fn degraded_conditioner_passes_through_raw() -> Result<(), String> {
        const RATE: u32 = 48_000;
        let mut conditioner = AudioConditioner::new(RATE)?;
        assert!(
            conditioner.take_failure().is_none(),
            "a healthy conditioner reports no failure"
        );

        // Healthy pass first: audio flows out, not just through.
        let healthy = corpus(1_920);
        let processed = conditioner.process(&healthy);
        assert_eq!(processed.len(), 1_920);
        assert!(
            processed.iter().any(|&sample| sample.abs() > 100),
            "conditioned tone must survive the pipeline audibly"
        );

        // Force the degradation switch (a live APM error cannot be
        // triggered on demand from pure Rust) and verify the contract.
        conditioner.disabled = true;
        let tail = vec![1_234_i16; 160];
        let passed = conditioner.process(&tail);
        assert_eq!(passed, &tail[..], "degraded pushes pass through raw");
        assert!(
            conditioner.flush().is_empty(),
            "degraded passes hold no remainder"
        );
        Ok(())
    }

    /// Rates that cannot form exact 10 ms frames are rejected at
    /// construction, never silently processed at the wrong frame length.
    #[test]
    fn rejects_non_frame_alignable_rates() {
        assert!(AudioConditioner::new(0).is_err());
        assert!(AudioConditioner::new(44_059).is_err());
        assert!(AudioConditioner::new(22_050).is_err());
    }

    /// The frame length derives from the sample rate through the
    /// processor's own 10 ms rule, not a hardcoded constant.
    #[test]
    fn frame_len_derives_from_sample_rate() -> Result<(), String> {
        assert_eq!(AudioConditioner::new(48_000)?.frame_len(), 480);
        assert_eq!(AudioConditioner::new(44_100)?.frame_len(), 441);
        assert_eq!(AudioConditioner::new(16_000)?.frame_len(), 160);
        Ok(())
    }
}

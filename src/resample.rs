//! Pure-Rust i16 audio resampling shared by the 413 batch retry and the
//! local whisper tier.
//! Integer ratio downsampling (48 kHz -> 16 kHz) decimates with a box
//! filter (mean of every `factor` samples); rates that do not divide
//! evenly fall back to linear interpolation; equal rates pass through
//! untouched; a zero rate is unresolvable and reports `None`.
//! Both consumers need 16 kHz mono i16 (whisper's native input format),
//! so the length math is documented per path below and pinned by tests.

/// Length math for integer-ratio decimation: `input_len` frames of
/// `factor` samples each, where the trailing frame may be shorter. Output
/// length is therefore `input_len.div_ceil(factor)`; a fully divisible
/// input yields exactly `input_len * target / source` samples.
const fn decimated_len(input_len: usize, factor: usize) -> usize {
    if factor == 0 {
        return input_len;
    }
    input_len.div_ceil(factor)
}

/// Length math for linear interpolation: the output samples positions
/// `0.0, step, 2*step, ...` along the input, where
/// `step = source_rate / target_rate`, and clamps at the last input
/// sample, so the output is `floor((input_len - 1) * target / source) + 1`
/// samples (and at least one for a non-empty input).
const fn interpolated_len(input_len: usize, source: u32, target: u32) -> usize {
    if input_len == 0 || source == 0 || target == 0 {
        return input_len;
    }
    let span = (input_len - 1).saturating_mul(target as usize);
    span / source as usize + 1
}

/// Mean of a frame of `i16` samples. The mean of `i16` values always fits
/// `i16`, so the `try_from` fallback is unreachable in practice.
pub(crate) fn mean_sample(frame: &[i16]) -> i16 {
    if frame.is_empty() {
        return 0;
    }
    let sum: i32 = frame.iter().copied().map(i32::from).sum();
    let len = i32::try_from(frame.len()).unwrap_or(i32::MAX);
    i16::try_from(sum / len).unwrap_or(i16::MAX)
}

/// Linear resample for capture rates that do not divide evenly into the
/// target rate (44.1 kHz, for example), or for upsampling. Output length
/// follows [`interpolated_len`].
#[allow(
    clippy::cast_possible_truncation,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss
)]
pub(crate) fn linear_resample(samples: &[i16], source_rate: u32, target_rate: u32) -> Vec<i16> {
    if samples.len() < 2 {
        return samples.to_vec();
    }
    let step = f64::from(source_rate) / f64::from(target_rate);
    let output_len = interpolated_len(samples.len(), source_rate, target_rate);
    let mut resampled = Vec::with_capacity(output_len);
    for index in 0..output_len {
        let position = index as f64 * step;
        let base = position.floor();
        let start = base as usize;
        if start + 1 >= samples.len() {
            resampled.push(samples[samples.len() - 1]);
            break;
        }
        let weight = position - base;
        let start_value = f64::from(samples[start]);
        let end_value = f64::from(samples[start + 1]);
        let value = (end_value - start_value)
            .mul_add(weight, start_value)
            .round();
        resampled.push(value.clamp(f64::from(i16::MIN), f64::from(i16::MAX)) as i16);
    }
    resampled
}

/// Resample `i16` mono audio from `source_rate` to `target_rate`.
/// Integer-ratio downsampling decimates with a box filter (48 kHz ->
/// 16 kHz averages every three samples, cutting the payload ~3x), rates
/// that do not divide evenly get a linear resample, equal rates pass
/// through untouched, and a zero rate is unresolvable (`None`). The
/// integer-ratio path averages the trailing partial frame over its own
/// length, so the output length is `ceil(input_len / factor)`; see
/// [`decimated_len`].
pub(crate) fn resample_i16(
    samples: &[i16],
    source_rate: u32,
    target_rate: u32,
) -> Option<Vec<i16>> {
    if source_rate == 0 || target_rate == 0 {
        return None;
    }
    if source_rate == target_rate {
        return Some(samples.to_vec());
    }
    if source_rate.is_multiple_of(target_rate) {
        let factor = usize::try_from(source_rate / target_rate).unwrap_or(usize::MAX);
        let mut resampled = Vec::with_capacity(decimated_len(samples.len(), factor));
        resampled.extend(samples.chunks(factor).map(mean_sample));
        return Some(resampled);
    }
    Some(linear_resample(samples, source_rate, target_rate))
}

#[cfg(test)]
mod tests {
    use super::{decimated_len, interpolated_len, resample_i16};

    #[test]
    fn decimation_reduces_48k_to_16k_by_exact_thirds() {
        // 48 samples of 3 kHz-ish pattern: 16 frames of 3 samples each,
        // every frame averaging to a distinct constant so each output
        // sample proves which input frame produced it.
        let samples: Vec<i16> = (0..16_i16)
            .flat_map(|frame| [frame, frame, frame])
            .collect();
        let resampled =
            resample_i16(&samples, 48_000, 16_000).unwrap_or_else(|| Vec::from([i16::MAX]));
        assert_eq!(resampled.len(), 16);
        for (index, sample) in resampled.iter().enumerate() {
            assert_eq!(*sample, i16::try_from(index).unwrap_or(i16::MAX));
        }
    }

    #[test]
    fn decimation_lengths_documented_for_exact_and_partial_frames() {
        // Fully divisible: input_len * 16000/48000.
        assert_eq!(decimated_len(48, 3), 16);
        assert_eq!(decimated_len(48_000, 3), 16_000);
        // Remainder frames average over their own length: ceil(input/3).
        assert_eq!(decimated_len(47, 3), 16);
        assert_eq!(decimated_len(1, 3), 1);
        // End-to-end through the resampler with the same rounding.
        let divisible = vec![4_i16; 30];
        let resampled = resample_i16(&divisible, 48_000, 16_000).unwrap_or_default();
        assert_eq!(resampled.len(), 10);
        let partial_tail = vec![6_i16; 31];
        let resampled_tail = resample_i16(&partial_tail, 48_000, 16_000).unwrap_or_default();
        assert_eq!(resampled_tail.len(), 11);
        // The tail frame (one sample) survives as the final output.
        assert_eq!(resampled_tail.last().copied(), Some(6));
    }

    #[test]
    fn frame_alignment_round_trips_a_ramp_without_phase_drift() {
        // A monotonic input decimated by exact thirds keeps every third
        // position, so the output stays monotonic and index-aligned: the
        // first output derives from the first frame, not from a later one.
        let samples: Vec<i16> = (0..30_i16).collect();
        let resampled = resample_i16(&samples, 48_000, 16_000).unwrap_or_default();
        let expected_means: Vec<i16> = (0..10_i16).map(|frame| frame * 3 + 1).collect();
        assert_eq!(resampled, expected_means);
    }

    #[test]
    fn linear_lengths_documented_for_non_integer_rates() {
        // floor((len - 1) * target / source) + 1.
        assert_eq!(interpolated_len(2, 44_100, 16_000), 1);
        assert_eq!(interpolated_len(44_101, 44_100, 16_000), 16_001);
        let samples = vec![0_i16; 100];
        let resampled = resample_i16(&samples, 44_100, 16_000).unwrap_or_default();
        assert_eq!(resampled.len(), interpolated_len(100, 44_100, 16_000));
        // Endpoints are pinned: a constant input stays constant.
        assert!(resampled.iter().all(|sample| *sample == 0));
    }

    #[test]
    fn equal_rates_pass_through_and_zero_rates_are_unresolvable() {
        let samples = vec![5, -5, 7, 9];
        let passed = resample_i16(&samples, 16_000, 16_000).unwrap_or_default();
        assert_eq!(passed, samples);
        assert!(resample_i16(&samples, 0, 16_000).is_none());
        assert!(resample_i16(&samples, 48_000, 0).is_none());
        assert!(resample_i16(&[], 48_000, 16_000).is_some());
    }
}

import { describe, expect, it } from 'vitest';

import { formatMismatch } from '../src/errors';
import { CHANNELS, SAMPLE_RATE_HZ } from '../src/format';

describe('formatMismatch', () => {
  it('accepts the format both clients ask for', () => {
    expect(formatMismatch({ sampleRate: SAMPLE_RATE_HZ, channels: CHANNELS })).toBeNull();
  });

  it('reports a wrong sample rate rather than letting Transcribe hang', () => {
    // The failure this guards is TASK-020's: Transcribe answers a disagreeing
    // rate by hanging, so nothing downstream would ever say what went wrong.
    const error = formatMismatch({ sampleRate: 44_100, channels: CHANNELS });

    expect(error?.code).toBe('SAMPLE_RATE_UNSUPPORTED');
    expect(error?.detail).toEqual({
      requested: { sampleRate: SAMPLE_RATE_HZ, channels: CHANNELS },
      actual: { sampleRate: 44_100, channels: CHANNELS },
    });
  });

  it('reports a wrong channel count', () => {
    const error = formatMismatch({ sampleRate: SAMPLE_RATE_HZ, channels: 2 });

    expect(error?.code).toBe('CHANNELS_UNSUPPORTED');
    expect(error?.detail?.actual.channels).toBe(2);
  });

  it('checks the sample rate first when both are wrong', () => {
    // Not arbitrary: the rate is the one that fails silently, so it is the one
    // a provider needs named. The channel count at least produces audio.
    expect(formatMismatch({ sampleRate: 48_000, channels: 2 })?.code).toBe(
      'SAMPLE_RATE_UNSUPPORTED',
    );
  });

  it('says what the device did and what is required, in both messages', () => {
    // These strings reach a provider through TASK-025 and TASK-070. They carry
    // numbers and nothing else — never the session token, never audio.
    const rate = formatMismatch({ sampleRate: 8_000, channels: CHANNELS });
    const channels = formatMismatch({ sampleRate: SAMPLE_RATE_HZ, channels: 6 });

    expect(rate?.message).toContain('8000');
    expect(rate?.message).toContain('16000');
    expect(channels?.message).toContain('6');
    expect(channels?.message).toContain('1');
  });
});

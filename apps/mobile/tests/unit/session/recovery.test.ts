import type { AudioCaptureErrorCode } from '@medauth/audio-wire';

import { recoveryFor } from '../../../src/session/recovery';

/**
 * Every code in the shared vocabulary.
 *
 * The `satisfies` clause is what keeps this list honest: adding a code to
 * `packages/audio-wire` without adding it here fails typechecking, which is the
 * same guarantee `recoveryFor`'s exhaustive switch gives on the other side.
 */
const RECOVERIES = {
  PERMISSION_DENIED: 'permission',
  SAMPLE_RATE_UNSUPPORTED: 'unsupported',
  CHANNELS_UNSUPPORTED: 'unsupported',
  ENDIANNESS_UNSUPPORTED: 'unsupported',
  CAPTURE_FAILED: 'retry',
  CAPTURE_TIMED_OUT: 'retry',
  AUTH_REJECTED: 'remint',
  SEND_BACKLOG_EXCEEDED: 'retry',
  STREAM_FAILED: 'partial',
} satisfies Record<AudioCaptureErrorCode, string>;

const CODES = Object.keys(RECOVERIES) as AudioCaptureErrorCode[];

describe('recoveryFor', () => {
  it.each(CODES)('maps %s to the action a provider can actually take', (code) => {
    expect(recoveryFor(code).kind).toBe(RECOVERIES[code]);
  });

  it('offers no retry for a device that cannot capture the format', () => {
    // Retrying is guaranteed to fail the same way — the rate, channel count and
    // byte order are all properties of the hardware, not of this attempt.
    for (const code of CODES) {
      const isFormatFailure = RECOVERIES[code] === 'unsupported';
      expect(recoveryFor(code).kind === 'unsupported').toBe(isFormatFailure);
    }
  });

  it('keeps a dropped stream distinct from one that never opened', () => {
    // SEND_BACKLOG_EXCEEDED means nothing was transmitted; STREAM_FAILED means
    // part of the encounter reached the server. A provider is owed a different
    // sentence in each case.
    expect(recoveryFor('SEND_BACKLOG_EXCEEDED').kind).toBe('retry');
    expect(recoveryFor('STREAM_FAILED').kind).toBe('partial');
  });

  it('gives an unknown code the safe answer rather than falling through', () => {
    // Unreachable through the type; reachable if a value crosses a boundary
    // untyped, and the answer must still be "nothing recorded, offer a retry".
    expect(recoveryFor('SOMETHING_NEW' as AudioCaptureErrorCode)).toEqual({ kind: 'retry' });
  });
});

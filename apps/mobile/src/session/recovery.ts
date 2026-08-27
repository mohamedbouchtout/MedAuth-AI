/**
 * What a provider can do about a capture failure.
 *
 * Every code in `packages/audio-wire`'s vocabulary blocks the visit — that part
 * is not a per-code decision and is enforced in `visitPhase`. What differs is
 * what to offer next, and offering the wrong thing has a cost in both
 * directions: inviting a retry on hardware that cannot capture 16kHz mono
 * produces a loop that will never succeed, and withholding one after a failure
 * that would clear on a second attempt ends a visit that did not need to end.
 *
 * The switch below is exhaustive over `AudioCaptureErrorCode`. That is the
 * mechanism, not a formality: a code added to the shared vocabulary fails
 * typechecking here until someone decides what the provider should be told,
 * which is the compile-time version of the rule that no code may quietly fall
 * through to a screen that looks like it is recording.
 */

import type { AudioCaptureErrorCode } from '@medauth/audio-wire';

export type Recovery =
  /** Retry the capture as-is. Nothing was recorded. */
  | { kind: 'retry' }
  /** A permissions problem — send the provider to settings, then retry. */
  | { kind: 'permission' }
  /** The token was refused. Re-mint for the same session, then retry. */
  | { kind: 'remint' }
  /** This hardware cannot capture what Transcribe needs. Retrying cannot help. */
  | { kind: 'unsupported' }
  /** Part of the encounter did reach the server; a retry resumes, it does not recover. */
  | { kind: 'partial' };

export function recoveryFor(code: AudioCaptureErrorCode): Recovery {
  switch (code) {
    case 'PERMISSION_DENIED':
      return { kind: 'permission' };

    // Properties of the device, not of this visit: the rate, the channel count
    // and the byte order are all fixed before a provider can act on them.
    case 'SAMPLE_RATE_UNSUPPORTED':
    case 'CHANNELS_UNSUPPORTED':
    case 'ENDIANNESS_UNSUPPORTED':
      return { kind: 'unsupported' };

    // All three mean nothing was transmitted and the encounter never started,
    // so a second attempt starts from the same place the first one did.
    case 'CAPTURE_FAILED':
    case 'CAPTURE_TIMED_OUT':
    case 'SEND_BACKLOG_EXCEEDED':
      return { kind: 'retry' };

    case 'AUTH_REJECTED':
      return { kind: 'remint' };

    case 'STREAM_FAILED':
      return { kind: 'partial' };

    default: {
      // Unreachable while the switch stays exhaustive — this line is what fails
      // the build if the vocabulary grows a code with no decision behind it.
      const unhandled: never = code;
      void unhandled;
      // A value from outside the type still gets the safe answer rather than a
      // fallthrough: nothing recorded, offer a retry, never reach "recording".
      return { kind: 'retry' };
    }
  }
}

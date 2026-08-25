/**
 * The failure vocabulary for audio capture, shared by every client.
 *
 * Every one of these is *returned* in the hook's state, never thrown. A thrown
 * error is what a React error boundary swallows, and a swallowed capture
 * failure would recreate the exact problem this module exists to prevent: a
 * provider believing an encounter is being recorded when it is not.
 *
 * One vocabulary rather than one per app, because the screens that surface
 * these — TASK-025 on mobile, TASK-070 on web — owe a provider the same answer
 * to the same failure, and a code that exists on one platform only is still
 * worth naming in one place. Which codes a given platform can actually emit is
 * noted on the codes themselves.
 */

import { CHANNELS, REQUESTED_FORMAT, SAMPLE_RATE_HZ, type AudioFormat } from './format';

export type AudioCaptureErrorCode =
  /** The provider declined the microphone, or the OS refused it. */
  | 'PERMISSION_DENIED'
  /** The hardware delivered a sample rate we did not ask for. Transcribe would hang. */
  | 'SAMPLE_RATE_UNSUPPORTED'
  /** The hardware delivered a channel count we did not ask for. */
  | 'CHANNELS_UNSUPPORTED'
  /**
   * This platform is big-endian; the PCM would reach Transcribe byte-swapped.
   *
   * Only the mobile path can produce this. The browser writes its own samples
   * with an explicit little-endian `DataView`, so there is nothing for the host
   * byte order to get wrong — see `pcm.ts`.
   */
  | 'ENDIANNESS_UNSUPPORTED'
  /** The microphone stream itself failed to start. */
  | 'CAPTURE_FAILED'
  /**
   * Capture started and then no audio ever arrived.
   *
   * Distinct from `CAPTURE_FAILED`, which means the microphone refused to start
   * and said so. This is the quieter failure: everything reported success and
   * nothing came. In a browser the usual cause is an `AudioContext` that is
   * suspended because the page never had user activation, and a suspended
   * context does not error — it simply never runs the worklet. Without a
   * deadline the hook would sit in `starting` forever, which is the silent hang
   * this whole vocabulary exists to make impossible.
   */
  | 'CAPTURE_TIMED_OUT'
  /**
   * The WebSocket never opened. TASK-020 refuses a bad session token *before*
   * the handshake completes, so a rejected JWT arrives here as an upgrade that
   * failed rather than as a close with code 4401 — re-mint the session before
   * retrying rather than treating it as a network blip.
   */
  | 'AUTH_REJECTED'
  /**
   * The socket never opened and audio piled up past the cap while waiting.
   *
   * Distinct from `STREAM_FAILED` on purpose: nothing has been transmitted, the
   * encounter has not started, and retrying may well work. `STREAM_FAILED`
   * means a working connection dropped partway through, which is a different
   * conversation to have with a provider.
   */
  | 'SEND_BACKLOG_EXCEEDED'
  /** A socket that had opened and was carrying audio failed or closed. */
  | 'STREAM_FAILED';

export interface AudioFormatDetail {
  requested: { sampleRate: number; channels: number };
  actual: { sampleRate: number; channels: number };
}

export interface AudioCaptureError {
  code: AudioCaptureErrorCode;
  /**
   * Operator-facing text. Never contains the session token, and never contains
   * audio or anything derived from it.
   */
  message: string;
  /** Present for the two format mismatches, so a screen can say what differed. */
  detail?: AudioFormatDetail;
}

/**
 * Compare a delivered format against the one requested.
 *
 * Returns the error to report, or null when the format is usable. Kept as a
 * function rather than inlined because it is applied twice — once against the
 * rate the stream reports after `start()`, and again against the first buffer
 * actually delivered — and the two must not be allowed to disagree.
 */
export function formatMismatch(actual: AudioFormat): AudioCaptureError | null {
  if (actual.sampleRate !== SAMPLE_RATE_HZ) {
    return {
      code: 'SAMPLE_RATE_UNSUPPORTED',
      message: `This device captured audio at ${actual.sampleRate} Hz; MedAuth AI requires ${SAMPLE_RATE_HZ} Hz.`,
      detail: { requested: REQUESTED_FORMAT, actual },
    };
  }
  if (actual.channels !== CHANNELS) {
    return {
      code: 'CHANNELS_UNSUPPORTED',
      message: `This device captured ${actual.channels} channels; MedAuth AI requires ${CHANNELS}.`,
      detail: { requested: REQUESTED_FORMAT, actual },
    };
  }
  return null;
}

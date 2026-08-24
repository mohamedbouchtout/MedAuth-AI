/**
 * The failure vocabulary for audio capture.
 *
 * Every one of these is *returned* in the hook's state, never thrown. A thrown
 * error is what a React error boundary swallows, and a swallowed capture
 * failure would recreate the exact problem this module exists to prevent: a
 * provider believing an encounter is being recorded when it is not.
 */

import { CHANNELS, REQUESTED_FORMAT, SAMPLE_RATE_HZ, type AudioFormat } from './format';
export type AudioCaptureErrorCode =
  /** The provider declined the microphone, or the OS refused it. */
  | 'PERMISSION_DENIED'
  /** The hardware delivered a sample rate we did not ask for. Transcribe would hang. */
  | 'SAMPLE_RATE_UNSUPPORTED'
  /** The hardware delivered a channel count we did not ask for. */
  | 'CHANNELS_UNSUPPORTED'
  /** This platform is big-endian; the PCM would reach Transcribe byte-swapped. */
  | 'ENDIANNESS_UNSUPPORTED'
  /** The microphone stream itself failed to start. */
  | 'CAPTURE_FAILED'
  /**
   * The WebSocket never opened. TASK-020 refuses a bad session token *before*
   * the handshake completes, so a rejected JWT arrives here as an upgrade that
   * failed rather than as a close with code 4401 — re-mint the session before
   * retrying rather than treating it as a network blip.
   */
  | 'AUTH_REJECTED'
  /** The socket opened and then failed, or audio outran it while connecting. */
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

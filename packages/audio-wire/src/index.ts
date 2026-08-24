/**
 * `@medauth/audio-wire` — the encounter-audio wire contract.
 *
 * Everything a client needs to agree with `services/audio-ingestion` about what
 * goes over the socket, and nothing about how any one platform captures it.
 * `apps/mobile` (TASK-022) and `apps/web` (TASK-023) both import from here; no
 * copy of these constants or of `PcmFramer` should exist in either app.
 */

export {
  BYTES_PER_SAMPLE,
  CHANNELS,
  CHUNK_BYTES,
  CHUNK_DURATION_MS,
  ENCODING,
  MAX_PENDING_BYTES,
  REQUESTED_FORMAT,
  SAMPLE_RATE_HZ,
  formatOf,
  isLittleEndian,
  type AudioFormat,
} from './format';

export { PcmFramer, PendingAudioOverflow } from './framing';

export {
  formatMismatch,
  type AudioCaptureError,
  type AudioCaptureErrorCode,
  type AudioFormatDetail,
} from './errors';

export { floatToInt16LE } from './pcm';

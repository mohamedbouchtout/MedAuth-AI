/**
 * The one description of the wire format, shared by every client that streams
 * encounter audio and by their tests.
 *
 * These values are not free choices. `audio-ingestion` forwards whatever it
 * receives straight to AWS Transcribe Medical, which is configured from
 * `TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ` and `TRANSCRIBE_MEDICAL_MEDIA_ENCODING`
 * in `.env.example` (16000 and `pcm`). TASK-020 recorded that Transcribe
 * answers a sample rate disagreeing with the audio by *hanging rather than
 * erroring*, so a mismatch here is not a degraded stream — it is a stream that
 * never produces a transcript and never says why.
 *
 * It lives in a package rather than in one app because both `apps/mobile`
 * (TASK-022) and `apps/web` (TASK-023) send to the same endpoint and must agree
 * byte for byte. Two hand-maintained copies of a wire contract drift, for the
 * same reason `packages/api-envelope` exists.
 */

/** Hz. Must equal `TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ`. */
export const SAMPLE_RATE_HZ = 16_000;

/** Mono. Transcribe Medical streaming takes a single channel. */
export const CHANNELS = 1;

/**
 * 16-bit signed samples.
 *
 * Doubles as `expo-audio`'s `encoding` option value on the mobile side, which
 * is a convenience rather than a coincidence worth relying on: the constant is
 * here because it describes the bytes on the wire.
 */
export const ENCODING = 'int16' as const;

/** Two bytes per sample, because the encoding is int16. */
export const BYTES_PER_SAMPLE = 2;

/** The chunk cadence TASK-022 and TASK-023 both specify. */
export const CHUNK_DURATION_MS = 250;

/**
 * 8000 bytes: 16000 samples/s x 2 bytes x 0.25 s x 1 channel.
 *
 * Neither platform hands audio over in this size — `expo-audio`'s `onBuffer`
 * delivers whatever the native layer picks, and an `AudioWorkletProcessor`
 * delivers 128-sample render quanta — so this is the boundary both re-chunk to.
 */
export const CHUNK_BYTES =
  (SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * CHANNELS * CHUNK_DURATION_MS) / 1000;

/**
 * How much audio may pile up while the WebSocket is still connecting: five
 * seconds' worth. Past this the connection is not coming, and continuing to
 * accumulate would trade a visible failure for unbounded memory growth holding
 * PHI. Failing is the better half of that trade.
 *
 * **Five seconds is a round-number default, not a measured value.** Nothing has
 * yet observed real handshake times from a device or a browser — there is no
 * session screen on either platform to produce them until TASK-025 and
 * TASK-070 — so it was picked to sit clearly between two bounds: long enough
 * that no healthy handshake on any network could reach it, short enough that a
 * provider is told the encounter is not being recorded while the visit is still
 * starting rather than minutes in. It is safe to change once real connect-time
 * data exists; treat it as a placeholder for that measurement rather than as a
 * tuned figure.
 *
 * Note it is a byte cap, not a timer. Nothing counts seconds — the bound is
 * reached when this many bytes are held, which is five seconds only because the
 * capture format is fixed above. Change the format and the wall-clock meaning
 * changes with it.
 */
export const MAX_PENDING_BYTES = SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * CHANNELS * 5;

/** The format a client asks the microphone for. */
export interface AudioFormat {
  sampleRate: number;
  channels: number;
}

export const REQUESTED_FORMAT: AudioFormat = {
  sampleRate: SAMPLE_RATE_HZ,
  channels: CHANNELS,
};

/**
 * True when this platform lays int16 out little-endian.
 *
 * Transcribe Medical requires 16-bit signed *little-endian* PCM. This matters
 * only where a platform hands over int16 bytes it produced itself — which is
 * the mobile path, where `expo-audio` fills the buffer natively. Both mobile
 * targets are little-endian in practice, which is exactly what makes it worth
 * asserting rather than assuming: the failure would otherwise be inaudible
 * noise reaching the transcriber, not a crash.
 *
 * The browser path does not need this guard, because it converts float samples
 * itself through `DataView.setInt16(..., true)`, which is little-endian
 * whatever the host is. See `pcm.ts`.
 */
export function isLittleEndian(): boolean {
  return new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;
}

/** Describes a captured buffer's format so it can be compared to what we asked for. */
export function formatOf(buffer: { sampleRate: number; channels: number }): AudioFormat {
  return { sampleRate: buffer.sampleRate, channels: buffer.channels };
}

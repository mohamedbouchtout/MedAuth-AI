/**
 * The one description of the wire format, shared by the hook and its tests.
 *
 * These values are not free choices. `audio-ingestion` forwards whatever it
 * receives straight to AWS Transcribe Medical, which is configured from
 * `TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ` and `TRANSCRIBE_MEDICAL_MEDIA_ENCODING`
 * in `.env.example` (16000 and `pcm`). TASK-020 recorded that Transcribe
 * answers a sample rate disagreeing with the audio by *hanging rather than
 * erroring*, so a mismatch here is not a degraded stream — it is a stream that
 * never produces a transcript and never says why.
 */

/** Hz. Must equal `TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ`. */
export const SAMPLE_RATE_HZ = 16_000;

/** Mono. Transcribe Medical streaming takes a single channel. */
export const CHANNELS = 1;

/** `expo-audio`'s PCM encoding option. 16-bit signed samples. */
export const ENCODING = 'int16' as const;

/** Two bytes per sample, because the encoding is int16. */
export const BYTES_PER_SAMPLE = 2;

/** The chunk cadence TASK-022 specifies. */
export const CHUNK_DURATION_MS = 250;

/**
 * 8000 bytes: 16000 samples/s x 2 bytes x 0.25 s x 1 channel.
 *
 * `useAudioStream` exposes no buffer-size or interval control, so `onBuffer`
 * delivers whatever size the native layer picks and this is the boundary we
 * re-chunk to ourselves.
 */
export const CHUNK_BYTES =
  (SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * CHANNELS * CHUNK_DURATION_MS) / 1000;

/**
 * How much audio may pile up while the WebSocket is still connecting, five
 * seconds' worth. Past this the connection is not coming, and continuing to
 * accumulate would trade a visible failure for unbounded memory growth holding
 * PHI. Failing is the better half of that trade.
 */
export const MAX_PENDING_BYTES = SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * CHANNELS * 5;

/** The format this app asks the microphone for. */
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
 * Transcribe Medical requires 16-bit signed *little-endian* PCM. Both target
 * platforms are little-endian in practice, which is exactly what makes this
 * worth asserting rather than assuming: the failure would otherwise be
 * inaudible noise reaching the transcriber, not a crash.
 */
export function isLittleEndian(): boolean {
  return new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;
}

/** Describes a captured buffer's format so it can be compared to what we asked for. */
export function formatOf(buffer: { sampleRate: number; channels: number }): AudioFormat {
  return { sampleRate: buffer.sampleRate, channels: buffer.channels };
}

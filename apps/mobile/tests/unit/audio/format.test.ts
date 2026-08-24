import {
  CHANNELS,
  CHUNK_BYTES,
  ENCODING,
  MAX_PENDING_BYTES,
  REQUESTED_FORMAT,
  SAMPLE_RATE_HZ,
  formatOf,
  isLittleEndian,
} from '../../../src/audio/format';

describe('wire format constants', () => {
  it('matches TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ and _MEDIA_ENCODING', () => {
    // These mirror .env.example. Transcribe answers a disagreement by hanging,
    // so a drift here produces silence rather than an error.
    expect(SAMPLE_RATE_HZ).toBe(16_000);
    expect(CHANNELS).toBe(1);
    expect(ENCODING).toBe('int16');
  });

  it('caps pending audio at five seconds', () => {
    expect(MAX_PENDING_BYTES).toBe(SAMPLE_RATE_HZ * 2 * CHANNELS * 5);
    expect(MAX_PENDING_BYTES / CHUNK_BYTES).toBe(20);
  });

  it('describes what was requested', () => {
    expect(REQUESTED_FORMAT).toEqual({ sampleRate: 16_000, channels: 1 });
  });
});

describe('isLittleEndian', () => {
  it('is true on the platforms this app targets', () => {
    // Both iOS and Android are little-endian; Transcribe requires 16-bit signed
    // little-endian PCM. Asserted because the failure would be inaudible noise
    // reaching the transcriber rather than a crash.
    expect(isLittleEndian()).toBe(true);
  });
});

describe('formatOf', () => {
  it('reduces a captured buffer to the two fields that are compared', () => {
    expect(formatOf({ sampleRate: 44_100, channels: 2 })).toEqual({
      sampleRate: 44_100,
      channels: 2,
    });
  });
});

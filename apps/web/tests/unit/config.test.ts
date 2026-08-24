import { describe, expect, it } from 'vitest';

import { AUDIO_INGESTION_WS_URL, isInsecureOrigin } from '../../src/config';

describe('AUDIO_INGESTION_WS_URL', () => {
  it('defaults to the local audio-ingestion port from CLAUDE.md', () => {
    expect(AUDIO_INGESTION_WS_URL).toBe('ws://localhost:8001');
  });
});

describe('isInsecureOrigin', () => {
  it('flags a plaintext origin', () => {
    // CLAUDE.md requires TLS everywhere; ws:// is a localhost-only convenience
    // and a deployed build must be caught setting it.
    expect(isInsecureOrigin('ws://audio.example')).toBe(true);
  });

  it('accepts a TLS origin', () => {
    expect(isInsecureOrigin('wss://audio.example')).toBe(false);
  });
});

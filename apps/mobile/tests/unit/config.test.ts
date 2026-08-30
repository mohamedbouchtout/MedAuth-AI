import {
  API_BASE_URL,
  AUDIO_INGESTION_WS_URL,
  NUDGE_SERVICE_WS_URL,
  TRACK_B_RAG_URL,
  isInsecureOrigin,
} from '../../src/config';

describe('AUDIO_INGESTION_WS_URL', () => {
  it('defaults to the local audio-ingestion port from CLAUDE.md', () => {
    expect(AUDIO_INGESTION_WS_URL).toBe('ws://localhost:8001');
  });
});

describe('API_BASE_URL', () => {
  it('defaults to the local track-a-clinical port from CLAUDE.md', () => {
    // track-a-clinical owns session lifecycle, so that is the service the
    // session screen posts to.
    expect(API_BASE_URL).toBe('http://localhost:8003');
  });

  it('is a separate origin from the audio socket', () => {
    // Reusing the WebSocket origin for HTTP would put a ws:// scheme in front of
    // a REST path — a failure that reads like a routing bug rather than a
    // configuration one.
    expect(API_BASE_URL).not.toBe(AUDIO_INGESTION_WS_URL);
  });
});

describe('NUDGE_SERVICE_WS_URL', () => {
  it('defaults to the local nudge-service port from CLAUDE.md', () => {
    expect(NUDGE_SERVICE_WS_URL).toBe('ws://localhost:8005');
  });

  it('is a separate origin from the audio socket', () => {
    // Two services, two sockets. One origin serving both would work locally by
    // accident and fail in every deployed environment.
    expect(NUDGE_SERVICE_WS_URL).not.toBe(AUDIO_INGESTION_WS_URL);
  });
});

describe('TRACK_B_RAG_URL', () => {
  it('defaults to the local track-b-rag port from CLAUDE.md', () => {
    // track-b-rag owns the nudge acknowledge route (TASK-041b), which is what
    // the banner's dismiss button calls.
    expect(TRACK_B_RAG_URL).toBe('http://localhost:8002');
  });

  /**
   * The gap TASK-043 found. `.env.example` said in writing that this app needed
   * only the session-lifecycle origin, which stopped being true the moment the
   * banner grew a dismiss button. Pointing one variable at both services would
   * send the acknowledge call to track-a-clinical, which has no such route — a
   * 404 that reads like a broken endpoint rather than a misconfigured origin.
   */
  it('is a separate origin from the session lifecycle service', () => {
    expect(TRACK_B_RAG_URL).not.toBe(API_BASE_URL);
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

  it('flags a plaintext HTTP origin too', () => {
    // The start-visit body carries a patient identifier and the response carries
    // a session credential; one rule, one helper, so neither scheme is exempt.
    expect(isInsecureOrigin('http://api.example')).toBe(true);
  });

  it('accepts an HTTPS origin', () => {
    expect(isInsecureOrigin('https://api.example')).toBe(false);
  });
});

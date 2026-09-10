import {
  API_BASE_URL,
  AUDIO_INGESTION_WS_URL,
  NUDGE_SERVICE_WS_URL,
  SMART_ISS,
  SMART_RETURN_URI,
  TRACK_B_RAG_URL,
  isInsecureOrigin,
} from '../../src/config';

import appManifest from '../../app.json';

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

describe('SMART_ISS', () => {
  /**
   * Empty is the honest default. There is no issuer a build could assume: an
   * `iss` is per-practice as well as per-vendor, so a plausible-looking default
   * would launch every unconfigured deployment at somebody else's EHR.
   */
  it('is empty until a deployment names an EHR', () => {
    expect(SMART_ISS).toBe('');
  });
});

describe('SMART_RETURN_URI', () => {
  it('defaults to the scheme this app registers', () => {
    expect(SMART_RETURN_URI).toBe('medauth://launch');
  });

  /**
   * The drift this guards is silent in both directions. `openAuthSessionAsync`
   * is told which redirect ends the session, and the OS routes a URL back only
   * to an app that registered its scheme — so a scheme here that `app.json` does
   * not declare leaves the browser on a page nothing handles, which the app
   * cannot tell from a provider closing the login window.
   *
   * It does not prove the OS actually routes it: nothing in Jest exercises that,
   * and it is verified by hand per TASK-025c. What it does prove is that the two
   * files this repository controls still agree.
   */
  it('uses the scheme registered in app.json', () => {
    const scheme = appManifest.expo.scheme;
    expect(scheme).toBeTruthy();
    expect(SMART_RETURN_URI.startsWith(`${scheme}://`)).toBe(true);
  });

  /**
   * The claim code is appended as this target's query string, and
   * fhir-integration validates the same property on its own copy — a target
   * that already carried a query would silently produce two.
   */
  it('carries no query string or fragment', () => {
    expect(SMART_RETURN_URI).not.toContain('?');
    expect(SMART_RETURN_URI).not.toContain('#');
  });

  /**
   * `isInsecureOrigin` deliberately does not answer for this one. It is a custom
   * scheme rather than a network origin — the OS routes it back to this app and
   * nothing is transmitted — so measuring it against TLS would be a category
   * error, and an `https://` value here would open a web page instead of
   * returning to the app.
   */
  it('is a custom scheme rather than an HTTP origin', () => {
    expect(SMART_RETURN_URI.startsWith('http://')).toBe(false);
    expect(SMART_RETURN_URI.startsWith('https://')).toBe(false);
  });
});

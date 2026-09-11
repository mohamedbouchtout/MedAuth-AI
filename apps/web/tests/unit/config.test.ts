import { describe, expect, it } from 'vitest';

import {
  AUDIO_INGESTION_WS_URL,
  FHIR_INTEGRATION_URL,
  SMART_ISS,
  isInsecureOrigin,
} from '../../src/config';

describe('AUDIO_INGESTION_WS_URL', () => {
  it('defaults to the local audio-ingestion port from CLAUDE.md', () => {
    expect(AUDIO_INGESTION_WS_URL).toBe('ws://localhost:8001');
  });
});

describe('FHIR_INTEGRATION_URL', () => {
  it('defaults to the local fhir-integration port from CLAUDE.md', () => {
    expect(FHIR_INTEGRATION_URL).toBe('http://localhost:8004');
  });

  /**
   * The three HTTP origins name three different services, and a build that
   * reuses one for another fails in a way that reads like a routing bug rather
   * than a configuration one. Asserted rather than assumed because the failure
   * is quiet: a launch-context call sent to track-a-clinical is a 404, which is
   * also what an expired launch answers.
   */
  it('is not either of the other two HTTP origins', async () => {
    const { API_BASE_URL, TRACK_B_RAG_URL } = await import('../../src/config');

    expect(FHIR_INTEGRATION_URL).not.toBe(API_BASE_URL);
    expect(FHIR_INTEGRATION_URL).not.toBe(TRACK_B_RAG_URL);
  });
});

describe('SMART_ISS', () => {
  /**
   * Empty is a real state rather than a missing default: a deployment that has
   * not named an EHR offers no standalone launch and says so. Defaulting to
   * some issuer would launch against an EHR nobody configured, and defaulting to
   * a placeholder would fail at SMART discovery in a way that reads as the EHR
   * being down.
   */
  it('is empty when no issuer is configured', () => {
    expect(SMART_ISS).toBe('');
  });
});

describe('isInsecureOrigin', () => {
  it('flags a plaintext WebSocket origin', () => {
    // CLAUDE.md requires TLS everywhere; ws:// is a localhost-only convenience
    // and a deployed build must be caught setting it.
    expect(isInsecureOrigin('ws://audio.example')).toBe(true);
  });

  it('accepts a TLS WebSocket origin', () => {
    expect(isInsecureOrigin('wss://audio.example')).toBe(false);
  });

  /**
   * The http:// half is what TASK-070 added. This app has three HTTP origins and
   * the check tested only `ws://`, so every one of them would have been reported
   * secure — including the one that carries a patient's name in a query string.
   * `apps/mobile`'s copy of this helper has covered both schemes since it gained
   * its own HTTP origins, and one rule answered differently on two platforms is
   * how an origin ends up quietly exempt.
   */
  it('flags a plaintext HTTP origin', () => {
    expect(isInsecureOrigin('http://fhir.example')).toBe(true);
  });

  it('accepts a TLS HTTP origin', () => {
    expect(isInsecureOrigin('https://fhir.example')).toBe(false);
  });
});

import { describe, expect, it, vi } from 'vitest';

import { createSessionsApi, type FetchLike } from '../src/sessions';

const BASE = 'https://api.example';
const SESSION_ID = '11111111-1111-4111-8111-111111111111';
const PROVIDER_ID = '22222222-2222-4222-8222-222222222222';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function envelope(data: unknown): unknown {
  return { data, error: null };
}

function errorEnvelope(code: string, message: string): unknown {
  return { data: null, error: { code, message } };
}

function apiWith(fetchImpl: FetchLike) {
  return createSessionsApi(BASE, fetchImpl);
}

describe('startVisit', () => {
  it('posts the wire field names and returns the session', async () => {
    const fetchImpl = vi.fn<FetchLike>(async () =>
      jsonResponse(201, envelope({ session_id: SESSION_ID, jwt: 'a.b.c' })),
    );

    const result = await apiWith(fetchImpl).startVisit({
      patientId: 'patient-1',
      providerId: PROVIDER_ID,
    });

    expect(result).toEqual({ ok: true, value: { sessionId: SESSION_ID, jwt: 'a.b.c' } });
    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe(`${BASE}/sessions/start`);
    // `patient_id` is the wire name for the model's patient_fhir_id column.
    expect(JSON.parse(init.body as string)).toEqual({
      patient_id: 'patient-1',
      provider_id: PROVIDER_ID,
    });
  });

  it('sends the launch and encounter ids that fill the payer columns', async () => {
    // Both, or neither is any use: the launch supplies the EHR credential and
    // the encounter id says which visit to ask about. Sending one alone leaves
    // insurance_payer NULL, and every policy query for the visit then reports
    // missing parameters rather than answering.
    const fetchImpl = vi.fn<FetchLike>(async () =>
      jsonResponse(201, envelope({ session_id: SESSION_ID, jwt: 'a.b.c' })),
    );

    await apiWith(fetchImpl).startVisit({
      patientId: 'patient-1',
      providerId: PROVIDER_ID,
      ehrEncounterId: 'Encounter/9',
      launchId: 'launch-7',
    });

    expect(JSON.parse(fetchImpl.mock.calls[0]![1].body as string)).toEqual({
      patient_id: 'patient-1',
      provider_id: PROVIDER_ID,
      ehr_encounter_id: 'Encounter/9',
      launch_id: 'launch-7',
    });
  });

  it('omits launch_id when the visit was started outside a launch', async () => {
    const fetchImpl = vi.fn<FetchLike>(async () =>
      jsonResponse(201, envelope({ session_id: SESSION_ID, jwt: 'a.b.c' })),
    );

    await apiWith(fetchImpl).startVisit({ patientId: 'p', providerId: PROVIDER_ID });

    expect(JSON.parse(fetchImpl.mock.calls[0]![1].body as string)).not.toHaveProperty('launch_id');
  });

  it('omits ehr_encounter_id when there is none', async () => {
    const fetchImpl = vi.fn<FetchLike>(async () =>
      jsonResponse(201, envelope({ session_id: SESSION_ID, jwt: 'a.b.c' })),
    );

    await apiWith(fetchImpl).startVisit({ patientId: 'p', providerId: PROVIDER_ID });

    expect(JSON.parse(fetchImpl.mock.calls[0]![1].body as string)).not.toHaveProperty(
      'ehr_encounter_id',
    );
  });

  it('reports the envelope error on a failure status', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(422, errorEnvelope('validation_error', 'provider_id is not a UUID'));

    const result = await apiWith(fetchImpl).startVisit({ patientId: 'p', providerId: 'nope' });

    expect(result).toEqual({
      ok: false,
      failure: { kind: 'status', status: 422, code: 'validation_error', message: expect.any(String) },
    });
  });

  it('reports an unreachable server without surfacing the thrown value', async () => {
    const fetchImpl: FetchLike = async () => {
      throw new Error('getaddrinfo ENOTFOUND api.internal.example');
    };

    const result = await apiWith(fetchImpl).startVisit({ patientId: 'p', providerId: PROVIDER_ID });

    expect(result.ok).toBe(false);
    if (result.ok) {
      throw new Error('unreachable');
    }
    expect(result.failure.kind).toBe('network');
    // The thrown value can name an internal host; it must not reach a provider.
    expect(result.failure.message).not.toContain('api.internal.example');
  });

  it.each([
    ['a body that is not the envelope', jsonResponse(201, { session: SESSION_ID })],
    ['a session id that is not a string', jsonResponse(201, envelope({ session_id: 1, jwt: 'a' }))],
    ['a missing token', jsonResponse(201, envelope({ session_id: SESSION_ID }))],
  ])('reports %s as malformed rather than half-reading it', async (_label, response) => {
    const result = await apiWith(async () => response).startVisit({
      patientId: 'p',
      providerId: PROVIDER_ID,
    });

    expect(result).toEqual({ ok: false, failure: { kind: 'malformed', message: expect.any(String) } });
  });
});

describe('remintToken', () => {
  it('carries the session token as a bearer header, never in the URL', async () => {
    const fetchImpl = vi.fn<FetchLike>(async () =>
      jsonResponse(200, envelope({ session_id: SESSION_ID, jwt: 'fresh.token.value' })),
    );

    const result = await apiWith(fetchImpl).remintToken(SESSION_ID, 'stale.token.value');

    expect(result).toEqual({ ok: true, value: { sessionId: SESSION_ID, jwt: 'fresh.token.value' } });
    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe(`${BASE}/sessions/${SESSION_ID}/token`);
    expect(url).not.toContain('stale.token.value');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer stale.token.value');
  });

  it('surfaces the 409 status so a caller can tell "visit is over" from "refresh failed"', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(409, errorEnvelope('session_completed', 'Session is already completed'));

    const result = await apiWith(fetchImpl).remintToken(SESSION_ID, 'stale.token.value');

    expect(result).toEqual({
      ok: false,
      failure: { kind: 'status', status: 409, code: 'session_completed', message: expect.any(String) },
    });
  });

  it('falls back to a status-only failure when the error body is unreadable', async () => {
    const fetchImpl: FetchLike = async () =>
      ({
        ok: false,
        status: 502,
        json: async () => {
          throw new Error('not json');
        },
      }) as unknown as Response;

    const result = await apiWith(fetchImpl).remintToken(SESSION_ID, 'stale.token.value');

    expect(result).toEqual({
      ok: false,
      failure: { kind: 'status', status: 502, code: 'unknown', message: expect.any(String) },
    });
  });
});

describe('endVisit', () => {
  it('posts to the end route and succeeds without a body', async () => {
    const fetchImpl = vi.fn<FetchLike>(async () =>
      jsonResponse(200, envelope({ session_id: SESSION_ID, status: 'completed' })),
    );

    const result = await apiWith(fetchImpl).endVisit(SESSION_ID);

    expect(result).toEqual({ ok: true, value: undefined });
    expect(fetchImpl.mock.calls[0]![0]).toBe(`${BASE}/sessions/${SESSION_ID}/end`);
    expect(fetchImpl.mock.calls[0]![1].method).toBe('POST');
  });

  it('reports a 404 for an unknown session', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(404, errorEnvelope('session_not_found', 'No encounter for session'));

    const result = await apiWith(fetchImpl).endVisit(SESSION_ID);

    expect(result.ok).toBe(false);
  });
});

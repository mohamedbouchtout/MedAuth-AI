import type { FetchLike } from '@medauth/session-client';

import { LAUNCH_ID_HEADER, createFhirApi } from '../../../src/api/fhir';

/**
 * The fhir-integration client (TASK-025b).
 *
 * Two things matter beyond parsing: that the launch travels in the header rather
 * than the query string — it resolves to an EHR access token, so it is a
 * credential — and that a partial answer is read as a partial answer rather than
 * as an absence. A launch with a null `patient_id` is a standalone launch, not a
 * broken one.
 */

const BASE = 'https://fhir-integration.test';
const LAUNCH_ID = 'launch-7';
const PROVIDER_ID = '22222222-2222-4222-8222-222222222222';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function envelope(data: unknown): unknown {
  return { data, error: null };
}

function errorEnvelope(code: string, message: string): unknown {
  return { data: null, error: { code, message } };
}

function apiWith(fetchImpl: FetchLike) {
  return createFhirApi(BASE, fetchImpl);
}

describe('getLaunchContext', () => {
  it('carries the launch in the header and never the query string', async () => {
    const fetchImpl = jest.fn<Promise<Response>, Parameters<FetchLike>>(async () =>
      jsonResponse(200, envelope({ patient_id: 'p1', encounter_id: 'e1', provider_id: PROVIDER_ID })),
    );

    await apiWith(fetchImpl).getLaunchContext(LAUNCH_ID);

    const [url, init] = fetchImpl.mock.calls[0]!;
    // A launch_id resolves to an EHR access token. A query string is the one
    // place a credential is certain to be logged by intermediaries.
    expect(url).toBe(`${BASE}/fhir/launch-context`);
    expect(url).not.toContain(LAUNCH_ID);
    expect((init.headers as Record<string, string>)[LAUNCH_ID_HEADER]).toBe(LAUNCH_ID);
  });

  it('reads all three identifiers', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(200, envelope({ patient_id: 'p1', encounter_id: 'e1', provider_id: PROVIDER_ID }));

    const result = await apiWith(fetchImpl).getLaunchContext(LAUNCH_ID);

    expect(result).toEqual({
      ok: true,
      value: { patientId: 'p1', encounterId: 'e1', providerId: PROVIDER_ID },
    });
  });

  it('reads a standalone launch as nulls rather than as a failure', async () => {
    // A launch with a patient and no encounter is an ordinary standalone launch.
    // Treating it as malformed would tell a client to repeat a launch that works.
    const fetchImpl: FetchLike = async () =>
      jsonResponse(200, envelope({ patient_id: null, encounter_id: null, provider_id: PROVIDER_ID }));

    const result = await apiWith(fetchImpl).getLaunchContext(LAUNCH_ID);

    expect(result).toEqual({
      ok: true,
      value: { patientId: null, encounterId: null, providerId: PROVIDER_ID },
    });
  });

  it('reports the envelope error on a failure status', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(404, errorEnvelope('FHIR_UNKNOWN_LAUNCH', 'No such SMART launch.'));

    const result = await apiWith(fetchImpl).getLaunchContext(LAUNCH_ID);

    expect(result).toEqual({
      ok: false,
      failure: {
        kind: 'status',
        status: 404,
        code: 'FHIR_UNKNOWN_LAUNCH',
        message: 'No such SMART launch.',
      },
    });
  });

  it('reports a network failure without surfacing the thrown value', async () => {
    const fetchImpl: FetchLike = async () => {
      throw new Error(`connect ECONNREFUSED ${BASE}/fhir/launch-context`);
    };

    const result = await apiWith(fetchImpl).getLaunchContext(LAUNCH_ID);

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.failure.kind).toBe('network');
      expect(result.failure.message).not.toContain(BASE);
    }
  });

  it('reports a body it cannot read as malformed', async () => {
    const fetchImpl: FetchLike = async () => jsonResponse(200, { data: null, error: null });

    const result = await apiWith(fetchImpl).getLaunchContext(LAUNCH_ID);

    expect(result).toEqual({ ok: false, failure: expect.objectContaining({ kind: 'malformed' }) });
  });
});

describe('searchPatients', () => {
  const match = {
    patient_id: 'synthea-123',
    family_name: 'Sanchez',
    given_names: ['Aurelio', 'Luis'],
    birth_date: '1962-04-17',
    gender: 'male',
  };

  it('sends the query, and the birth date only when there is one', async () => {
    const fetchImpl = jest.fn<Promise<Response>, Parameters<FetchLike>>(async () =>
      jsonResponse(200, envelope({ matches: [match], truncated: false })),
    );
    const api = apiWith(fetchImpl);

    await api.searchPatients(LAUNCH_ID, 'Sanchez');
    await api.searchPatients(LAUNCH_ID, 'Sanchez', '1962-04-17');

    expect(fetchImpl.mock.calls[0]![0]).toBe(`${BASE}/fhir/patient/search?query=Sanchez`);
    expect(fetchImpl.mock.calls[1]![0]).toBe(
      `${BASE}/fhir/patient/search?query=Sanchez&birth_date=1962-04-17`,
    );
  });

  it('treats an empty birth date as absent', async () => {
    // The picker's text input starts empty, and sending `birth_date=` would be
    // a 422 from the route's date pattern rather than an unfiltered search.
    const fetchImpl = jest.fn<Promise<Response>, Parameters<FetchLike>>(async () =>
      jsonResponse(200, envelope({ matches: [], truncated: false })),
    );

    await apiWith(fetchImpl).searchPatients(LAUNCH_ID, 'Sanchez', '');

    expect(fetchImpl.mock.calls[0]![0]).not.toContain('birth_date');
  });

  it('reads the matches and the truncation flag', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(200, envelope({ matches: [match], truncated: true }));

    const result = await apiWith(fetchImpl).searchPatients(LAUNCH_ID, 'Sanchez');

    expect(result).toEqual({
      ok: true,
      value: {
        truncated: true,
        matches: [
          {
            patientId: 'synthea-123',
            familyName: 'Sanchez',
            givenNames: ['Aurelio', 'Luis'],
            birthDate: '1962-04-17',
            gender: 'male',
          },
        ],
      },
    });
  });

  it('reads a match the EHR holds no name for', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(200, envelope({ matches: [{ patient_id: 'p9' }], truncated: false }));

    const result = await apiWith(fetchImpl).searchPatients(LAUNCH_ID, 'Sanchez');

    expect(result).toEqual({
      ok: true,
      value: {
        truncated: false,
        matches: [
          {
            patientId: 'p9',
            familyName: null,
            givenNames: [],
            birthDate: null,
            gender: null,
          },
        ],
      },
    });
  });

  it('drops a candidate with no id rather than failing the whole search', async () => {
    // The other matches are still usable, and the one the provider wants may
    // well be among them — dropping the whole answer would be worse.
    const fetchImpl: FetchLike = async () =>
      jsonResponse(200, envelope({ matches: [{ family_name: 'Nameless' }, match], truncated: false }));

    const result = await apiWith(fetchImpl).searchPatients(LAUNCH_ID, 'Sanchez');

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.matches.map((entry) => entry.patientId)).toEqual(['synthea-123']);
    }
  });

  it('reads no matches as an empty list, not as a failure', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(200, envelope({ matches: [], truncated: false }));

    const result = await apiWith(fetchImpl).searchPatients(LAUNCH_ID, 'Nobody');

    expect(result).toEqual({ ok: true, value: { matches: [], truncated: false } });
  });

  it('reports a payload with no matches array as malformed', async () => {
    const fetchImpl: FetchLike = async () => jsonResponse(200, envelope({ truncated: false }));

    const result = await apiWith(fetchImpl).searchPatients(LAUNCH_ID, 'Sanchez');

    expect(result).toEqual({ ok: false, failure: expect.objectContaining({ kind: 'malformed' }) });
  });
});

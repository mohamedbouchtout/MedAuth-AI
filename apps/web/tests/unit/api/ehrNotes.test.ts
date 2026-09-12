/**
 * The chart write-back client (TASK-071), against `POST /fhir/notes`.
 *
 * The two assertions that are requirements rather than plumbing: the body is
 * `{session_id}` and nothing else — a client that posted the note text would
 * save a round trip and produce no `READ_NOTE` row anywhere — and the launch
 * travels in a header, never in the URL, because it resolves to an EHR access
 * token and a query string is the one place a credential is certain to be logged
 * by intermediaries.
 */

import { LAUNCH_ID_HEADER } from '@medauth/fhir-client';
import { describe, expect, it, vi } from 'vitest';

import { createEhrNotesApi, type FetchLike } from '../../../src/api/ehrNotes';

const SESSION = '11111111-1111-4111-8111-111111111111';
const LAUNCH = 'launch-7';

function jsonResponse(body: unknown, status = 201): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response;
}

describe('filing a note to the chart', () => {
  it('sends only the session, with the launch in a header', async () => {
    const fetchImpl = vi.fn<FetchLike>(() =>
      Promise.resolve(
        jsonResponse({
          data: { session_id: SESSION, ehr_document_ref_id: 'DocumentReference/9' },
          error: null,
        }),
      ),
    );
    const api = createEhrNotesApi('http://fhir.test', fetchImpl);

    const result = await api.writeNote(SESSION, LAUNCH);

    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe('http://fhir.test/fhir/notes');
    expect(url).not.toContain(LAUNCH);
    expect(JSON.parse(String(init.body))).toEqual({ session_id: SESSION });
    expect((init.headers as Record<string, string>)[LAUNCH_ID_HEADER]).toBe(LAUNCH);
    expect(result).toEqual({
      ok: true,
      value: { sessionId: SESSION, ehrDocumentRefId: 'DocumentReference/9' },
    });
  });

  it('reports the service’s own code for a note already on the chart', async () => {
    const body = {
      data: null,
      error: { code: 'NOTE_ALREADY_WRITTEN_TO_EHR', message: 'already filed' },
    };
    const api = createEhrNotesApi('http://fhir.test', () => Promise.resolve(jsonResponse(body, 409)));

    const result = await api.writeNote(SESSION, LAUNCH);

    expect(result).toEqual({
      ok: false,
      failure: {
        kind: 'status',
        status: 409,
        code: 'NOTE_ALREADY_WRITTEN_TO_EHR',
        message: 'already filed',
      },
    });
  });

  /**
   * The one failure where the chart already changed. The message names the
   * created document, so it is carried through verbatim rather than replaced
   * with wording of this app's own.
   */
  it('carries the record-failure message through unchanged', async () => {
    const message = 'The note was filed as DocumentReference/9 but could not be recorded.';
    const body = { data: null, error: { code: 'EHR_NOTE_RECORD_FAILED', message } };
    const api = createEhrNotesApi('http://fhir.test', () => Promise.resolve(jsonResponse(body, 502)));

    const result = await api.writeNote(SESSION, LAUNCH);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.failure).toMatchObject({ code: 'EHR_NOTE_RECORD_FAILED', message });
  });

  it('reports a body it cannot read as malformed rather than as success', async () => {
    const api = createEhrNotesApi('http://fhir.test', () =>
      Promise.resolve(jsonResponse({ data: { session_id: SESSION }, error: null })),
    );

    const result = await api.writeNote(SESSION, LAUNCH);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.failure.kind).toBe('malformed');
  });

  it('does not surface a thrown value, which can carry the URL', async () => {
    const api = createEhrNotesApi('http://fhir.test', () =>
      Promise.reject(new Error('connect ECONNREFUSED launch-7')),
    );

    const result = await api.writeNote(SESSION, LAUNCH);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.failure.message).not.toContain(LAUNCH);
  });
});

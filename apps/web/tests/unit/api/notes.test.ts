/**
 * The note client (TASK-071), against track-a-clinical's `/notes/{session_id}`.
 *
 * Two properties matter more than the plumbing. The client must carry the
 * `null` / `[]` distinction on the code lists through to the caller intact — it
 * is the difference between "the extraction never answered" and "it ran and
 * found nothing", and a narrowing that flattened one into the other would put a
 * false statement about a patient's diagnoses on a review screen. And a `PATCH`
 * must put exactly the keys it was given on the wire, because the key set is the
 * whole message.
 */

import { describe, expect, it, vi } from 'vitest';

import { createNotesApi, type FetchLike } from '../../../src/api/notes';

const SESSION = '11111111-1111-4111-8111-111111111111';

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response;
}

const NOTE_BODY = {
  data: {
    session_id: SESSION,
    note_id: '22222222-2222-4222-8222-222222222222',
    soap_subjective: 'S',
    soap_objective: null,
    soap_assessment: 'A',
    soap_plan: 'P',
    icd10_codes: [
      {
        code: 'M17.11',
        display: 'Right knee OA',
        source: 'llm-extraction',
        confidence: null,
        validation: { source: 'comprehend-medical', confidence: 0.9, confirmed: true },
      },
    ],
    cpt_codes: null,
    generated_at: '2026-09-11T10:00:00Z',
    reviewed_by_provider: false,
    provider_edited: false,
    ehr_document_ref_id: null,
  },
  error: null,
};

describe('reading a note', () => {
  it('asks the session-keyed path and narrows the body', async () => {
    const fetchImpl = vi.fn<FetchLike>(() => Promise.resolve(jsonResponse(NOTE_BODY)));
    const api = createNotesApi('http://notes.test', fetchImpl);

    const result = await api.readNote(SESSION);

    expect(fetchImpl).toHaveBeenCalledWith(`http://notes.test/notes/${SESSION}`, undefined);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value.soapObjective).toBeNull();
    expect(result.value.icd10Codes?.[0]?.validation?.confirmed).toBe(true);
  });

  /** The distinction the whole shape contract turns on, carried to the caller. */
  it('keeps a null code list null and an empty one empty', async () => {
    const body = {
      ...NOTE_BODY,
      data: { ...NOTE_BODY.data, icd10_codes: [], cpt_codes: null },
    };
    const api = createNotesApi('http://notes.test', () => Promise.resolve(jsonResponse(body)));

    const result = await api.readNote(SESSION);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value.icd10Codes).toEqual([]);
    expect(result.value.cptCodes).toBeNull();
  });

  /**
   * A `source` outside the vocabulary makes the entry unreadable rather than
   * defaulting to one of the three. The value decides whether a code renders as
   * a suggestion or as something the provider is signing.
   */
  it('refuses a code whose source is not in the vocabulary', async () => {
    const body = {
      ...NOTE_BODY,
      data: {
        ...NOTE_BODY.data,
        icd10_codes: [{ code: 'M17.11', display: null, source: 'guessed', confidence: null }],
      },
    };
    const api = createNotesApi('http://notes.test', () => Promise.resolve(jsonResponse(body)));

    const result = await api.readNote(SESSION);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.failure.kind).toBe('malformed');
  });

  it('reports the envelope error on a 404', async () => {
    const body = { data: null, error: { code: 'note_not_generated', message: 'not yet' } };
    const api = createNotesApi('http://notes.test', () => Promise.resolve(jsonResponse(body, 404)));

    const result = await api.readNote(SESSION);

    expect(result).toEqual({
      ok: false,
      failure: { kind: 'status', status: 404, code: 'note_not_generated', message: 'not yet' },
    });
  });

  it('reports an unreachable server without surfacing the thrown value', async () => {
    const api = createNotesApi('http://notes.test', () =>
      Promise.reject(new Error('connect ECONNREFUSED http://notes.test/notes/secret')),
    );

    const result = await api.readNote(SESSION);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.failure.kind).toBe('network');
    expect(result.failure.message).not.toContain('secret');
  });
});

describe('editing a note', () => {
  it('sends exactly the keys it was given', async () => {
    const fetchImpl = vi.fn<FetchLike>(() => Promise.resolve(jsonResponse(NOTE_BODY)));
    const api = createNotesApi('http://notes.test', fetchImpl);

    await api.updateNote(SESSION, { soap_plan: 'MRI left knee.' });

    const init = fetchImpl.mock.calls[0]![1]!;
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(String(init.body))).toEqual({ soap_plan: 'MRI left knee.' });
  });

  /** `null` clears a column, and that has to survive serialisation as a key. */
  it('sends an explicit null rather than dropping the key', async () => {
    const fetchImpl = vi.fn<FetchLike>(() => Promise.resolve(jsonResponse(NOTE_BODY)));
    const api = createNotesApi('http://notes.test', fetchImpl);

    await api.updateNote(SESSION, { icd10_codes: null });

    const init = fetchImpl.mock.calls[0]![1]!;
    expect(JSON.parse(String(init.body))).toEqual({ icd10_codes: null });
  });

  /** The server answers 422 for a body that sets nothing; there is no point asking. */
  it('refuses an empty patch without making a request', async () => {
    const fetchImpl = vi.fn<FetchLike>(() => Promise.resolve(jsonResponse(NOTE_BODY)));
    const api = createNotesApi('http://notes.test', fetchImpl);

    const result = await api.updateNote(SESSION, {});

    expect(fetchImpl).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
  });
});

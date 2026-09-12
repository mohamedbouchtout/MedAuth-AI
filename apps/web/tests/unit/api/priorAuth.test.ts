/**
 * The prior-authorization client (TASK-072).
 *
 * Two things here are worth asserting beyond "it parses JSON": that the queue
 * read is always scoped to a provider, and that the resubmission goes to a
 * *different service* from the two reads. Getting the second wrong produces a
 * 404 against track-a-clinical that reads like a missing request rather than a
 * misrouted call.
 */

import { describe, expect, it, vi } from 'vitest';

import { createPriorAuthApi, narrowStatus, type FetchLike } from '../../../src/api/priorAuth';

const CLINICAL = 'http://clinical.test';
const PRIOR_AUTH = 'http://prior-auth.test';

function respondingWith(body: unknown, status = 200): { fetchImpl: FetchLike; urls: string[] } {
  const urls: string[] = [];
  const fetchImpl = vi.fn<FetchLike>((url) => {
    urls.push(url);
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });
  return { fetchImpl, urls };
}

function apiWith(fetchImpl: FetchLike) {
  return createPriorAuthApi({
    clinicalBaseUrl: CLINICAL,
    priorAuthBaseUrl: PRIOR_AUTH,
    fetchImpl,
  });
}

const ROW = {
  request_id: '44444444-4444-4444-8444-444444444444',
  session_id: '55555555-5555-4555-8555-555555555555',
  status: 'denied',
  payer_name: 'Aetna',
  payer_outcome: 'complete',
  submission_method: 'fhir-pas',
  submitted_at: '2026-09-02T09:00:00Z',
  decided_at: '2026-09-04T09:00:00Z',
  started_at: '2026-09-01T14:00:00Z',
  submittable: true,
};

describe('listRequests', () => {
  it('always sends the provider scope', async () => {
    const { fetchImpl, urls } = respondingWith({
      data: { requests: [ROW], next_cursor: null },
      error: null,
    });

    await apiWith(fetchImpl).listRequests('provider-1');

    expect(urls[0]).toContain('provider_id=provider-1');
  });

  it('reads the queue from track-a-clinical, not from prior-auth', async () => {
    const { fetchImpl, urls } = respondingWith({
      data: { requests: [], next_cursor: null },
      error: null,
    });

    await apiWith(fetchImpl).listRequests('provider-1');

    expect(urls[0]?.startsWith(`${CLINICAL}/prior-auth?`)).toBe(true);
  });

  it('passes the status filter and the cursor through', async () => {
    const { fetchImpl, urls } = respondingWith({
      data: { requests: [], next_cursor: null },
      error: null,
    });

    await apiWith(fetchImpl).listRequests('provider-1', { status: 'denied', cursor: 'abc' });

    expect(urls[0]).toContain('status=denied');
    expect(urls[0]).toContain('cursor=abc');
  });

  it('narrows a row and keeps the raw status alongside it', async () => {
    const { fetchImpl } = respondingWith({
      data: { requests: [ROW], next_cursor: 'next' },
      error: null,
    });

    const result = await apiWith(fetchImpl).listRequests('provider-1');

    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    expect(result.value.nextCursor).toBe('next');
    expect(result.value.requests[0]?.status).toBe('denied');
    expect(result.value.requests[0]?.rawStatus).toBe('denied');
    expect(result.value.requests[0]?.submittable).toBe(true);
  });

  it('reports a row it cannot read as malformed rather than guessing', async () => {
    // `submittable` decides whether a resubmit control is offered at all, so a
    // row missing it is not a row to render with a default.
    const { submittable, ...withoutFlag } = ROW;
    void submittable;
    const { fetchImpl } = respondingWith({
      data: { requests: [withoutFlag], next_cursor: null },
      error: null,
    });

    const result = await apiWith(fetchImpl).listRequests('provider-1');

    expect(result.ok).toBe(false);
  });

  it('surfaces the envelope error on a refusal', async () => {
    const { fetchImpl } = respondingWith(
      { data: null, error: { code: 'prior_auth_invalid_cursor', message: 'Start again.' } },
      422,
    );

    const result = await apiWith(fetchImpl).listRequests('provider-1', { cursor: 'bad' });

    expect(result.ok).toBe(false);
    if (result.ok) {
      return;
    }
    expect(result.failure).toMatchObject({ status: 422, code: 'prior_auth_invalid_cursor' });
  });

  it('reports an unreachable server without surfacing the thrown value', async () => {
    // A thrown fetch error can carry the request URL, which names a provider.
    const fetchImpl = vi.fn<FetchLike>(() => Promise.reject(new Error(`${CLINICAL}/prior-auth`)));

    const result = await apiWith(fetchImpl).listRequests('provider-1');

    expect(result.ok).toBe(false);
    if (result.ok) {
      return;
    }
    expect(result.failure.kind).toBe('network');
    expect(result.failure.message).not.toContain('provider');
  });
});

describe('readDecision', () => {
  it('reads from track-a-clinical', async () => {
    const { fetchImpl, urls } = respondingWith({
      data: {
        request_id: 'r1',
        payer_outcome: 'complete',
        payer_reference_number: null,
        decided_at: '2026-09-04T09:00:00Z',
        denial_reason: 'Not documented.',
      },
      error: null,
    });

    await apiWith(fetchImpl).readDecision('r1');

    expect(urls[0]).toBe(`${CLINICAL}/prior-auth/r1/decision`);
  });

  it('keeps a null denial reason as null', async () => {
    // The payer denied and gave no reason — a different fact from never having
    // been denied, and the screen renders them differently.
    const { fetchImpl } = respondingWith({
      data: {
        request_id: 'r1',
        payer_outcome: 'complete',
        payer_reference_number: null,
        decided_at: '2026-09-04T09:00:00Z',
        denial_reason: null,
      },
      error: null,
    });

    const result = await apiWith(fetchImpl).readDecision('r1');

    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    expect(result.value.denialReason).toBeNull();
    expect(result.value.decidedAt).not.toBeNull();
  });
});

describe('resubmit', () => {
  it('POSTs to prior-auth, which is a different service from the reads', async () => {
    const { fetchImpl, urls } = respondingWith({
      data: { request_id: 'r1', outcome: 'submitted', payer_outcome: 'queued' },
      error: null,
    });

    await apiWith(fetchImpl).resubmit('r1');

    expect(urls[0]).toBe(`${PRIOR_AUTH}/prior-auth/r1/submit`);
  });

  it('reads the manual outcome and the reason the router gave', async () => {
    // The reason exists only on this response: the router logs and returns it
    // without persisting it, which is TASK-072b.
    const { fetchImpl } = respondingWith({
      data: {
        request_id: 'r1',
        outcome: 'manual-submission-required',
        payer_outcome: null,
        submission_method: null,
        reason: 'payer-has-no-prior-auth-api',
      },
      error: null,
    });

    const result = await apiWith(fetchImpl).resubmit('r1');

    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    expect(result.value.outcome).toBe('manual-submission-required');
    expect(result.value.reason).toBe('payer-has-no-prior-auth-api');
    expect(result.value.payerOutcome).toBeNull();
  });

  it('surfaces a 409 rather than reporting a submission', async () => {
    const { fetchImpl } = respondingWith(
      {
        data: null,
        error: { code: 'prior_auth_not_submittable', message: 'The payer is holding it.' },
      },
      409,
    );

    const result = await apiWith(fetchImpl).resubmit('r1');

    expect(result.ok).toBe(false);
    if (result.ok) {
      return;
    }
    expect(result.failure).toMatchObject({ status: 409, code: 'prior_auth_not_submittable' });
  });
});

describe('narrowStatus', () => {
  it('keeps the six known statuses and calls anything else other', () => {
    expect(narrowStatus('manual-submission-required')).toBe('manual-submission-required');
    expect(narrowStatus('error')).toBe('error');
    expect(narrowStatus('awaiting-peer-review')).toBe('other');
  });
});

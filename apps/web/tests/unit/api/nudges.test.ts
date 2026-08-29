import { describe, expect, it, vi } from 'vitest';

import { createNudgesApi, type FetchLike } from '../../../src/api/nudges';

const BASE = 'https://rag.example';
const NUDGE_ID = '0b7f0000-0000-4000-8000-000000000001';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function acknowledged(alreadyAcknowledged: boolean): unknown {
  return {
    data: {
      nudge_id: NUDGE_ID,
      acknowledged: true,
      acknowledged_at: '2026-08-29T12:00:00Z',
      already_acknowledged: alreadyAcknowledged,
    },
    error: null,
  };
}

describe('acknowledge', () => {
  it('patches the nudge with an explicit acknowledged:true body', async () => {
    const fetchImpl = vi.fn<FetchLike>(async () => jsonResponse(200, acknowledged(false)));

    const result = await createNudgesApi(BASE, fetchImpl).acknowledge(NUDGE_ID);

    expect(result).toEqual({
      ok: true,
      value: {
        nudgeId: NUDGE_ID,
        acknowledgedAt: '2026-08-29T12:00:00Z',
        alreadyAcknowledged: false,
      },
    });
    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe(`${BASE}/nudges/${NUDGE_ID}/acknowledge`);
    expect(init.method).toBe('PATCH');
    // Not an empty PATCH: TASK-041b takes the transition as a field so the
    // request is self-describing and can grow one later without breaking.
    expect(JSON.parse(init.body as string)).toEqual({ acknowledged: true });
  });

  it('never sends an Authorization header', async () => {
    // The route carries no credential in v1 and does not validate one. Sending a
    // bearer token to a route that ignores it is how a client comes to believe
    // it is authenticated — CLAUDE.md, "A route keyed on a resource rather than
    // a session follows the same v1 rule".
    const fetchImpl = vi.fn<FetchLike>(async () => jsonResponse(200, acknowledged(false)));

    await createNudgesApi(BASE, fetchImpl).acknowledge(NUDGE_ID);

    const headers = fetchImpl.mock.calls[0]![1].headers as Record<string, string>;
    expect(Object.keys(headers).map((key) => key.toLowerCase())).not.toContain('authorization');
  });

  it('reports a repeat as success, not as an error', async () => {
    // The route is idempotent: a second dismissal is a 200 carrying the original
    // timestamp. A client that treated it as a failure would show the provider a
    // problem where there is none.
    const fetchImpl = vi.fn<FetchLike>(async () => jsonResponse(200, acknowledged(true)));

    const result = await createNudgesApi(BASE, fetchImpl).acknowledge(NUDGE_ID);

    expect(result.ok && result.value.alreadyAcknowledged).toBe(true);
  });

  it('surfaces the service error code and message on a 404', async () => {
    const fetchImpl = vi.fn<FetchLike>(async () =>
      jsonResponse(404, {
        data: null,
        error: { code: 'nudge_not_found', message: 'No such nudge.' },
      }),
    );

    const result = await createNudgesApi(BASE, fetchImpl).acknowledge(NUDGE_ID);

    expect(result).toEqual({
      ok: false,
      failure: { kind: 'status', status: 404, code: 'nudge_not_found', message: 'No such nudge.' },
    });
  });

  it('falls back to a generic status failure when the error body is unreadable', async () => {
    const fetchImpl = vi.fn<FetchLike>(async () => jsonResponse(500, { nothing: 'useful' }));

    const result = await createNudgesApi(BASE, fetchImpl).acknowledge(NUDGE_ID);

    expect(result.ok).toBe(false);
    expect(!result.ok && result.failure.kind === 'status' && result.failure.code).toBe('unknown');
  });

  it('reports an unreachable server without surfacing the thrown value', async () => {
    // The thrown error can carry the request URL, and a provider can act on
    // "unreachable" but not on a stack trace.
    const fetchImpl = vi.fn<FetchLike>(async () => {
      throw new Error(`connect ECONNREFUSED ${BASE}/nudges`);
    });

    const result = await createNudgesApi(BASE, fetchImpl).acknowledge(NUDGE_ID);

    expect(!result.ok && result.failure.kind).toBe('network');
    expect(!result.ok && result.failure.message).not.toContain(BASE);
  });

  it('rejects a success body it cannot read', async () => {
    const fetchImpl = vi.fn<FetchLike>(async () => jsonResponse(200, { data: { nudge_id: 7 } }));

    const result = await createNudgesApi(BASE, fetchImpl).acknowledge(NUDGE_ID);

    expect(!result.ok && result.failure.kind).toBe('malformed');
  });

  it('rejects a success response whose body is not JSON', async () => {
    const fetchImpl = vi.fn<FetchLike>(
      async () =>
        ({
          ok: true,
          status: 200,
          json: async () => {
            throw new Error('not json');
          },
        }) as unknown as Response,
    );

    const result = await createNudgesApi(BASE, fetchImpl).acknowledge(NUDGE_ID);

    expect(!result.ok && result.failure.kind).toBe('malformed');
  });
});

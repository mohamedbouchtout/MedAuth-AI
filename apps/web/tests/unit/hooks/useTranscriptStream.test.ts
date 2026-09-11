import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiResult, Session, SessionsApi } from '../../../src/api/sessions';
import { useTranscriptStream } from '../../../src/hooks/useTranscriptStream';
import {
  FakeWebSocket,
  installFakeWebSocket,
  tokenExpiringAt,
  transcriptPayload,
} from '../../support/transcript';

/**
 * The transcript socket (TASK-070), against TASK-041d's relay.
 *
 * The token handling is deliberately the nudge hook's, so the tests that matter
 * most here are the ones about *this* stream: that the credential rides in the
 * subprotocol and never the URL, that a frame is de-duplicated on `result_id`
 * rather than on its words, and that a reconnect does not present the speech it
 * missed as speech that never happened.
 */

const SESSION_ID = '11111111-1111-4111-8111-111111111111';
const NOW = 1_700_000_000_000;
const BASE_URL = 'wss://nudges.example';

/**
 * A stable identity, deliberately hoisted.
 *
 * `now` is a dependency of the hook's effect, so an inline `() => NOW` is a new
 * function on every render and re-runs the effect — tearing down the socket
 * under test and replacing it — the moment any state changes. The app never hits
 * this because it passes no `now` and gets the default `Date.now`, whose
 * identity is stable, but a test that re-created it would silently be asserting
 * against a socket that had already been detached.
 */
const now = () => NOW;

/** A token with a comfortable margin: no proactive refresh should fire for it. */
const FRESH = tokenExpiringAt(NOW, 900);
/** Inside TOKEN_REFRESH_SKEW_MS of `exp`, so opening must refresh first. */
const STALE = tokenExpiringAt(NOW, 10);

function sessionsThatMint(jwt: string): SessionsApi {
  return {
    startVisit: vi.fn(),
    remintToken: vi.fn(
      async (): Promise<ApiResult<Session>> => ({
        ok: true,
        value: { sessionId: SESSION_ID, jwt },
      }),
    ),
    endVisit: vi.fn(),
  } as unknown as SessionsApi;
}

function sessionsThatRefuse(status: number): SessionsApi {
  return {
    startVisit: vi.fn(),
    remintToken: vi.fn(
      async (): Promise<ApiResult<Session>> => ({
        ok: false,
        failure: { kind: 'status', status, code: 'session_completed', message: 'The visit is over.' },
      }),
    ),
    endVisit: vi.fn(),
  } as unknown as SessionsApi;
}

function renderStream(jwt = FRESH, sessions: SessionsApi = sessionsThatMint(FRESH)) {
  return renderHook(() =>
    useTranscriptStream({ sessionId: SESSION_ID, jwt, baseUrl: BASE_URL, sessions, now }),
  );
}

let restore: () => void;

beforeEach(() => {
  restore = installFakeWebSocket();
});

afterEach(() => {
  restore();
});

describe('opening', () => {
  it('opens the transcript path, not the nudge one', () => {
    renderStream();

    expect(FakeWebSocket.last.url).toBe(`${BASE_URL}/ws/transcript/${SESSION_ID}`);
  });

  /**
   * The one carrier a browser has, and the one place a credential must never go.
   * A token in a query string is written to every intermediary's access log.
   */
  it('carries the token as a subprotocol and never in the URL', () => {
    renderStream();

    expect(FakeWebSocket.last.url).not.toContain(FRESH);
    expect(FakeWebSocket.last.protocols).toContain(`medauth.jwt.${FRESH}`);
  });

  it('reports open once the handshake completes', async () => {
    const { result } = renderStream();

    act(() => FakeWebSocket.last.onopen?.());

    await waitFor(() => expect(result.current.state.status).toBe('open'));
  });
});

describe('receiving speech', () => {
  it('renders a stabilized segment', async () => {
    const { result } = renderStream();
    act(() => FakeWebSocket.last.onopen?.());

    act(() => {
      FakeWebSocket.last.onmessage?.({ data: transcriptPayload({ text: 'Knee pain.' }) });
    });

    await waitFor(() => expect(result.current.segments).toHaveLength(1));
    expect(result.current.segments[0]?.text).toBe('Knee pain.');
  });

  /**
   * Keyed on `result_id` and never on text equality. Transcribe reuses one id
   * across an utterance's revisions, and two utterances can legitimately carry
   * identical words — "Okay." twice in one consultation is not a duplicate.
   */
  it('de-duplicates on result id, not on the words', async () => {
    const { result } = renderStream();
    act(() => FakeWebSocket.last.onopen?.());

    act(() => {
      FakeWebSocket.last.onmessage?.({ data: transcriptPayload({ resultId: 'a', text: 'Okay.' }) });
      FakeWebSocket.last.onmessage?.({ data: transcriptPayload({ resultId: 'a', text: 'Okay.' }) });
      FakeWebSocket.last.onmessage?.({ data: transcriptPayload({ resultId: 'b', text: 'Okay.' }) });
    });

    await waitFor(() => expect(result.current.segments).toHaveLength(2));
  });

  it('drops a frame it cannot parse without failing the stream', async () => {
    const { result } = renderStream();
    act(() => FakeWebSocket.last.onopen?.());

    act(() => {
      FakeWebSocket.last.onmessage?.({ data: 'not json' });
      FakeWebSocket.last.onmessage?.({ data: transcriptPayload({ text: 'Knee pain.' }) });
    });

    await waitFor(() => expect(result.current.segments).toHaveLength(1));
    expect(result.current.state.status).toBe('open');
  });

  it('ignores a binary frame', async () => {
    const { result } = renderStream();
    act(() => FakeWebSocket.last.onopen?.());

    act(() => FakeWebSocket.last.onmessage?.({ data: new ArrayBuffer(8) }));

    await waitFor(() => expect(result.current.segments).toHaveLength(0));
  });
});

describe('losing the connection', () => {
  /**
   * The message has to say that the gap is permanent. The bus keeps no history,
   * so reconnecting resumes from the present and the speech that happened while
   * the socket was down is not recoverable from anywhere — a provider told only
   * "reconnecting" would reasonably assume it would catch up.
   */
  it('says the missed speech will not appear, not merely that it dropped', async () => {
    const { result } = renderStream();
    act(() => FakeWebSocket.last.onopen?.());
    act(() => FakeWebSocket.last.onclose?.());

    await waitFor(() => expect(result.current.state.status).toBe('error'));
    if (result.current.state.status !== 'error') {
      throw new Error('expected an error state');
    }
    expect(result.current.state.error.code).toBe('STREAM_FAILED');
    expect(result.current.state.error.message).toContain('will not appear here');
  });

  it('retry opens a fresh socket', async () => {
    const { result } = renderStream();
    act(() => FakeWebSocket.last.onopen?.());
    act(() => FakeWebSocket.last.onclose?.());
    await waitFor(() => expect(result.current.state.status).toBe('error'));

    const before = FakeWebSocket.instances.length;
    act(() => result.current.retry());

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(before + 1));
  });
});

describe('token freshness', () => {
  it('re-mints before opening with a token near expiry', async () => {
    const sessions = sessionsThatMint(FRESH);
    renderStream(STALE, sessions);

    await waitFor(() => expect(sessions.remintToken).toHaveBeenCalledWith(SESSION_ID, STALE));
    await waitFor(() => expect(FakeWebSocket.last.protocols).toContain(`medauth.jwt.${FRESH}`));
  });

  /**
   * The handshake is refused before it completes, so a rejected token arrives as
   * a close with nothing ever having opened. One refresh, then a retry — never a
   * second attempt with the token that was just refused.
   */
  it('re-mints once after a refused handshake, then gives up', async () => {
    const sessions = sessionsThatMint(FRESH);
    const { result } = renderStream(FRESH, sessions);

    act(() => FakeWebSocket.last.onclose?.());
    await waitFor(() => expect(sessions.remintToken).toHaveBeenCalledTimes(1));

    act(() => FakeWebSocket.last.onclose?.());
    await waitFor(() => expect(result.current.state.status).toBe('error'));
    if (result.current.state.status !== 'error') {
      throw new Error('expected an error state');
    }
    expect(result.current.state.error.code).toBe('AUTH_REJECTED');
    expect(sessions.remintToken).toHaveBeenCalledTimes(1);
  });

  /**
   * A 409 is the only status that ends the visit — the encounter is already
   * completed, so there is nothing left to refresh a token for. Everything else
   * leaves the encounter open and is a failure to connect, not a finished visit.
   */
  it('reports a completed visit distinctly from a refused refresh', async () => {
    const { result } = renderStream(STALE, sessionsThatRefuse(409));

    await waitFor(() => expect(result.current.state.status).toBe('error'));
    if (result.current.state.status !== 'error') {
      throw new Error('expected an error state');
    }
    expect(result.current.state.error.code).toBe('VISIT_COMPLETED');
  });

  it('reports any other refusal as a failed refresh', async () => {
    const { result } = renderStream(STALE, sessionsThatRefuse(503));

    await waitFor(() => expect(result.current.state.status).toBe('error'));
    if (result.current.state.status !== 'error') {
      throw new Error('expected an error state');
    }
    expect(result.current.state.error.code).toBe('REFRESH_FAILED');
  });
});

describe('changing session', () => {
  /**
   * One encounter's speech must never be shown under another's heading. The
   * segments are cleared during the render that sees the new identity, not in an
   * effect afterwards — an effect would leave the previous patient's words on
   * screen for a frame.
   */
  it('clears the previous encounter’s speech immediately', async () => {
    const sessions = sessionsThatMint(FRESH);
    const { result, rerender } = renderHook(
      ({ sessionId }: { sessionId: string }) =>
        useTranscriptStream({
          sessionId,
          jwt: FRESH,
          baseUrl: BASE_URL,
          sessions,
          now,
        }),
      { initialProps: { sessionId: SESSION_ID } },
    );

    act(() => FakeWebSocket.last.onopen?.());
    act(() => {
      FakeWebSocket.last.onmessage?.({ data: transcriptPayload({ text: 'Knee pain.' }) });
    });
    await waitFor(() => expect(result.current.segments).toHaveLength(1));

    rerender({ sessionId: '22222222-2222-4222-8222-222222222222' });

    expect(result.current.segments).toHaveLength(0);
    expect(result.current.state.status).toBe('connecting');
  });
});

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiResult, Session, SessionsApi } from '../../../src/api/sessions';
import type { NudgesApi } from '../../../src/api/nudges';
import { NudgeOverlay } from '../../../src/components/NudgeOverlay';
import { FakeWebSocket, installFakeWebSocket, nudgePayload, tokenExpiringAt } from '../../support/nudges';

const SESSION_ID = '11111111-1111-4111-8111-111111111111';
const NOW = 1_700_000_000_000;
const BASE_URL = 'wss://nudges.example';

/** A token with a comfortable margin: no proactive refresh should fire for it. */
const FRESH = tokenExpiringAt(NOW, 900);
/** Inside TOKEN_REFRESH_SKEW_MS of `exp`, so opening must refresh first. */
const STALE = tokenExpiringAt(NOW, 10);

function sessionsThatMint(jwt: string): SessionsApi {
  return {
    startVisit: vi.fn(),
    remintToken: vi.fn(async (): Promise<ApiResult<Session>> => ({
      ok: true,
      value: { sessionId: SESSION_ID, jwt },
    })),
    endVisit: vi.fn(),
  } as unknown as SessionsApi;
}

function sessionsThatRefuse(status: number, code = 'session_completed'): SessionsApi {
  return {
    startVisit: vi.fn(),
    remintToken: vi.fn(async (): Promise<ApiResult<Session>> => ({
      ok: false,
      failure: { kind: 'status', status, code, message: 'The visit is over.' },
    })),
    endVisit: vi.fn(),
  } as unknown as SessionsApi;
}

function nudgesThatAcknowledge(): NudgesApi {
  return {
    acknowledge: vi.fn(async (nudgeId: string) => ({
      ok: true as const,
      value: { nudgeId, acknowledgedAt: '2026-08-29T12:00:00Z', alreadyAcknowledged: false },
    })),
  };
}

function renderOverlay(props: Partial<ComponentProps<typeof NudgeOverlay>> = {}) {
  const sessions = props.sessions ?? sessionsThatMint(FRESH);
  const nudges = props.nudges ?? nudgesThatAcknowledge();
  const view = render(
    <NudgeOverlay
      sessionId={SESSION_ID}
      jwt={props.jwt ?? FRESH}
      baseUrl={BASE_URL}
      sessions={sessions}
      nudges={nudges}
      now={() => NOW}
    />,
  );
  return { ...view, sessions, nudges };
}

/** Deliver one payload on the live socket, as nudge-service would. */
async function receive(payload: unknown): Promise<void> {
  await act(async () => {
    FakeWebSocket.last.onopen?.();
    FakeWebSocket.last.onmessage?.({ data: JSON.stringify(payload) });
  });
}

let restoreWebSocket: () => void;

beforeEach(() => {
  restoreWebSocket = installFakeWebSocket();
});

afterEach(() => {
  restoreWebSocket();
});

describe('connecting', () => {
  it('renders nothing until something arrives', () => {
    renderOverlay();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('opens the nudge socket for this session, with the token as a subprotocol', () => {
    renderOverlay();

    expect(FakeWebSocket.last.url).toBe(`${BASE_URL}/ws/nudges/${SESSION_ID}`);
    // The version marker first, so the server has something safe to echo; the
    // token never in the URL, which is the one place it is certain to be logged.
    expect(FakeWebSocket.last.protocols).toEqual(['medauth.session.v1', `medauth.jwt.${FRESH}`]);
    expect(FakeWebSocket.last.url).not.toContain(FRESH);
  });
});

describe('rendering a nudge', () => {
  it('shows an alert when a nudge arrives', async () => {
    renderOverlay();

    await receive(nudgePayload());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('knee MRI');
    expect(alert).toHaveTextContent('CPT 73721');
    expect(alert).toHaveTextContent('six weeks of conservative therapy');
    expect(alert).toHaveTextContent('High denial risk');
  });

  it('renders a nudge that names no CPT code', async () => {
    // TASK-044 raises these from a keyword the resolver could not code. A
    // component that assumed a string would break on the first one.
    renderOverlay();

    await receive(nudgePayload({ cpt_code: null, procedure: 'arthroscopy' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('arthroscopy');
    expect(alert).not.toHaveTextContent('CPT');
  });

  it('says the criteria are unknown rather than implying nothing is missing', async () => {
    renderOverlay();

    await receive(nudgePayload({ missing_criteria: [] }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /No criteria list was available for this plan/,
    );
  });

  it('drops a malformed payload and keeps the socket open', async () => {
    renderOverlay();

    await act(async () => {
      FakeWebSocket.last.onopen?.();
      FakeWebSocket.last.onmessage?.({ data: 'not a nudge at all' });
    });

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(FakeWebSocket.last.closed).toBe(false);

    // And the next valid one still renders — the drop is not a terminal state.
    await act(async () => {
      FakeWebSocket.last.onmessage?.({ data: JSON.stringify(nudgePayload()) });
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('knee MRI');
  });

  it('shows one banner for a republished nudge', async () => {
    renderOverlay();

    await receive(nudgePayload());
    await act(async () => {
      FakeWebSocket.last.onmessage?.({ data: JSON.stringify(nudgePayload()) });
    });

    expect(screen.getAllByRole('alert')).toHaveLength(1);
  });
});

describe('dismissing', () => {
  it('acknowledges the nudge by id and removes the banner', async () => {
    const { nudges } = renderOverlay();

    await receive(nudgePayload());
    const button = await screen.findByRole('button', { name: 'Dismiss' });
    await act(async () => {
      fireEvent.click(button);
    });

    expect(nudges.acknowledge).toHaveBeenCalledWith('0b7f0000-0000-4000-8000-000000000001');
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });

  it('keeps the banner when the acknowledge fails', async () => {
    // A provider who clicked dismiss and saw the alert vanish would believe it
    // was recorded as seen, and the audit row standing in for a credential on
    // that route would not exist.
    const nudges: NudgesApi = {
      acknowledge: vi.fn(async () => ({
        ok: false as const,
        failure: { kind: 'network' as const, message: 'MedAuth AI could not reach the server.' },
      })),
    };
    renderOverlay({ nudges });

    await receive(nudgePayload());
    const button = await screen.findByRole('button', { name: 'Dismiss' });
    await act(async () => {
      fireEvent.click(button);
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be dismissed/);
  });

  it('moves focus to the next banner rather than dropping it on the body', async () => {
    renderOverlay();

    await receive(nudgePayload());
    await act(async () => {
      FakeWebSocket.last.onmessage?.({
        data: JSON.stringify(
          nudgePayload({ nudge_id: '0b7f0000-0000-4000-8000-000000000002', procedure: 'shoulder MRI' }),
        ),
      });
    });

    const [first] = screen.getAllByRole('button', { name: 'Dismiss' });
    await act(async () => {
      fireEvent.click(first!);
    });

    await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(1));
    expect(document.activeElement).not.toBe(document.body);
  });
});

describe('token freshness', () => {
  it('re-mints before opening when the held token is near expiry', async () => {
    // The nudge socket opens later in an encounter than the audio socket, and
    // SESSION_TTL_SECONDS is 15 minutes, so this is the ordinary case.
    const sessions = sessionsThatMint(FRESH);
    renderOverlay({ jwt: STALE, sessions });

    await waitFor(() => expect(sessions.remintToken).toHaveBeenCalledWith(SESSION_ID, STALE));
    await waitFor(() =>
      expect(FakeWebSocket.last.protocols).toEqual([
        'medauth.session.v1',
        `medauth.jwt.${FRESH}`,
      ]),
    );
    // Never /sessions/start: that forks one visit into two encounters.
    expect(sessions.startVisit).not.toHaveBeenCalled();
  });

  it('does not re-mint a token that is comfortably fresh', () => {
    const sessions = sessionsThatMint(FRESH);
    renderOverlay({ sessions });

    expect(sessions.remintToken).not.toHaveBeenCalled();
  });

  it('re-mints once and retries when the socket fails to open', async () => {
    // TASK-041 validates ahead of the handshake, so a refused token arrives as a
    // failed upgrade rather than as a close with code 4401.
    const sessions = sessionsThatMint(FRESH);
    renderOverlay({ sessions });

    const refused = FakeWebSocket.last;
    await act(async () => {
      refused.onclose?.();
    });

    await waitFor(() => expect(sessions.remintToken).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    expect(sessions.startVisit).not.toHaveBeenCalled();
  });

  it('gives up after one refresh rather than looping', async () => {
    const sessions = sessionsThatMint(FRESH);
    renderOverlay({ sessions });

    await act(async () => {
      FakeWebSocket.last.onclose?.();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    await act(async () => {
      FakeWebSocket.last.onclose?.();
    });

    expect(await screen.findByRole('status')).toHaveTextContent(/could not be refreshed|refused/);
    expect(sessions.remintToken).toHaveBeenCalledTimes(1);
  });

  it('reports the visit as over on a 409 and offers no reconnect', async () => {
    const sessions = sessionsThatRefuse(409);
    renderOverlay({ jwt: STALE, sessions });

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(/already been completed/);
    expect(screen.queryByRole('button', { name: 'Reconnect' })).not.toBeInTheDocument();
    expect(sessions.startVisit).not.toHaveBeenCalled();
  });

  it('offers a reconnect for a failure that is not a completed visit', async () => {
    const sessions = sessionsThatRefuse(503, 'unavailable');
    renderOverlay({ jwt: STALE, sessions });

    expect(await screen.findByRole('button', { name: 'Reconnect' })).toBeInTheDocument();
  });
});

describe('a stream that drops', () => {
  it('tells the provider rather than going quiet', async () => {
    // A silent overlay is exactly what "nothing to flag" looks like. A provider
    // must not read a dead socket as a clean encounter.
    renderOverlay();

    await receive(nudgePayload());
    await act(async () => {
      FakeWebSocket.last.onclose?.();
    });

    expect(await screen.findByRole('status')).toHaveTextContent(/dropped/);
  });

  it('reconnects when asked', async () => {
    renderOverlay();

    await receive(nudgePayload());
    await act(async () => {
      FakeWebSocket.last.onclose?.();
    });
    const reconnect = await screen.findByRole('button', { name: 'Reconnect' });
    await act(async () => {
      fireEvent.click(reconnect);
    });

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
  });
});

/**
 * TASK-043's suite.
 *
 * The cases here are the ones the task names, and each is written against the
 * decision it protects rather than against the implementation:
 *
 * - the haptic fires on `haptic: true`, and **does not** fire on a high-risk
 *   payload carrying `haptic: false` — the direct verification of TASK-040's
 *   decoupling decision, which nothing else would catch;
 * - both halves of the token freshness rule, and that `startVisit` is never the
 *   way a token is obtained;
 * - a 409 from the re-mint ends the visit rather than retrying;
 * - a nudge with no CPT code renders, and a malformed payload does not;
 * - dismissing calls the acknowledge route with the right body and no credential.
 *
 * Every socket here is the fake: React Native's WebSocket cannot be made to fail
 * an upgrade on demand, and a refused upgrade is the case the reactive half of
 * the freshness rule exists for.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { ApiResult, Session, SessionsApi } from '@medauth/session-client';

import { NudgeOverlay, type HapticsLike } from '../../../src/components/NudgeOverlay';
import type { NudgesApi } from '../../../src/api/nudges';
import {
  FakeWebSocket,
  fallbackNudgePayload,
  installFakeWebSocket,
  nudgePayload,
} from '../../support/nudges';
import { tokenExpiringAt } from '../../support/token';

const SESSION_ID = '11111111-1111-4111-8111-111111111111';
const NUDGE_ID = '0b7f0000-0000-4000-8000-000000000001';
const NOW_MS = 1_700_000_000_000;

/** Far enough from `exp` that the proactive refresh must not fire. */
function freshToken(): string {
  return tokenExpiringAt(NOW_MS, 900);
}

/** Inside the refresh skew, so the proactive half must fire before opening. */
function staleToken(): string {
  return tokenExpiringAt(NOW_MS, 5);
}

function session(jwt: string): Session {
  return { sessionId: SESSION_ID, jwt };
}

function ok<T>(value: T): ApiResult<T> {
  return { ok: true, value };
}

function status<T>(code: number): ApiResult<T> {
  return {
    ok: false,
    failure: { kind: 'status', status: code, code: 'conflict', message: 'The visit is over.' },
  };
}

interface Doubles {
  sessions: SessionsApi;
  startVisit: jest.Mock;
  remintToken: jest.Mock;
  nudges: NudgesApi;
  acknowledge: jest.Mock;
  haptics: HapticsLike;
  notificationAsync: jest.Mock;
}

function doubles(overrides: Partial<Record<'remint' | 'acknowledge', unknown>> = {}): Doubles {
  const startVisit = jest.fn();
  const remintToken = jest.fn().mockResolvedValue(overrides.remint ?? ok(session(freshToken())));
  const acknowledge = jest.fn().mockResolvedValue(
    overrides.acknowledge ??
      ok({ nudgeId: NUDGE_ID, acknowledgedAt: '2026-08-30T10:00:00Z', alreadyAcknowledged: false }),
  );
  const notificationAsync = jest.fn().mockResolvedValue(undefined);

  return {
    sessions: {
      startVisit,
      remintToken,
      endVisit: jest.fn(),
    } as unknown as SessionsApi,
    startVisit,
    remintToken,
    nudges: { acknowledge } as unknown as NudgesApi,
    acknowledge,
    haptics: { notificationAsync } as HapticsLike,
    notificationAsync,
  };
}

/**
 * Mount the overlay and let everything the mount started settle.
 *
 * `await act` rather than a bare `render`: the proactive half of the freshness
 * rule re-mints during mount, and that promise resolving outside `act` is a
 * state update React warns about and, worse, one a later assertion could race.
 */
async function renderOverlay(d: Doubles, jwt: string = freshToken()): Promise<void> {
  await act(async () => {
    render(
      <NudgeOverlay
        sessionId={SESSION_ID}
        jwt={jwt}
        baseUrl="wss://nudges.example"
        nudges={d.nudges}
        sessions={d.sessions}
        now={() => NOW_MS}
        haptics={d.haptics}
      />,
    );
  });
}

/**
 * Wait for the component to have constructed its socket.
 *
 * The hook opens it in an effect, and React flushes passive effects after the
 * render call returns rather than during it — so a test that reaches for the
 * socket immediately finds none. Every case here waits on the socket rather than
 * assuming it, which is also what makes the proactive-refresh cases honest: they
 * are asserting that a socket appeared *after* a re-mint resolved.
 */
async function openedSocket(): Promise<FakeWebSocket> {
  await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));
  return FakeWebSocket.last;
}

/** Open the socket the component constructed and deliver one payload. */
async function deliver(payload: Record<string, unknown> | string): Promise<void> {
  const socket = await openedSocket();
  await act(async () => {
    socket.fireOpen();
  });
  await act(async () => {
    socket.fireMessage(typeof payload === 'string' ? payload : JSON.stringify(payload));
  });
}

let restoreWebSocket: () => void;

beforeEach(() => {
  restoreWebSocket = installFakeWebSocket();
});

afterEach(() => {
  restoreWebSocket();
  jest.clearAllMocks();
});

describe('the haptic', () => {
  it('fires when the payload carries haptic true', async () => {
    const d = doubles();
    await renderOverlay(d);

    await deliver(nudgePayload());

    await waitFor(() => expect(d.notificationAsync).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId(`nudge-${NUDGE_ID}`)).toBeTruthy();
  });

  /**
   * The decision this test exists for.
   *
   * `query.fallback_answer()` reports `denial_risk: "high"` honestly — the
   * requirement genuinely is unverified — and the emitter withholds `haptic`
   * because the answer could not be checked. A client that re-derived the buzz
   * from the risk level would turn one Qdrant or Bedrock outage into a device
   * buzzing once per procedure in every concurrent encounter, every alert saying
   * only "confirm manually", until the physician learns to ignore the buzz that
   * means "this order will be denied".
   *
   * The nudge must still be rendered: the escalation is suppressed, not the
   * alert.
   */
  it('does not fire on a high-risk fallback answer carrying haptic false', async () => {
    const d = doubles();
    await renderOverlay(d);

    await deliver(fallbackNudgePayload());

    await waitFor(() => expect(screen.getByTestId(`nudge-${NUDGE_ID}`)).toBeTruthy());
    expect(screen.getByText('High denial risk')).toBeTruthy();
    expect(d.notificationAsync).not.toHaveBeenCalled();
  });

  it('buzzes once when the same nudge_id is republished', async () => {
    const d = doubles();
    await renderOverlay(d);

    await deliver(nudgePayload());
    await act(async () => {
      FakeWebSocket.last.fireMessage(JSON.stringify(nudgePayload()));
    });

    await waitFor(() => expect(d.notificationAsync).toHaveBeenCalledTimes(1));
    expect(screen.getAllByTestId(`nudge-${NUDGE_ID}`)).toHaveLength(1);
  });
});

describe('rendering', () => {
  it('renders a nudge with no CPT code without naming one', async () => {
    const d = doubles();
    await renderOverlay(d);

    await deliver(nudgePayload({ cpt_code: null, procedure: 'arthroscopy' }));

    await waitFor(() => expect(screen.getByText('arthroscopy')).toBeTruthy());
    expect(screen.queryByText(/CPT/)).toBeNull();
  });

  it('says the criteria are unknown when missing_criteria is empty', async () => {
    const d = doubles();
    await renderOverlay(d);

    await deliver(fallbackNudgePayload());

    await waitFor(() => expect(screen.getByTestId(`nudge-unknown-${NUDGE_ID}`)).toBeTruthy());
    expect(screen.queryByTestId(`nudge-criteria-${NUDGE_ID}`)).toBeNull();
  });

  it('drops a malformed payload and leaves the socket open', async () => {
    const d = doubles();
    await renderOverlay(d);

    await deliver('{"type":"PAYER_RULE_ALERT"');

    expect(screen.queryByTestId('nudge-overlay')).toBeNull();
    expect(FakeWebSocket.last.closed).toBe(false);
    expect(d.notificationAsync).not.toHaveBeenCalled();
  });
});

describe('dismissing', () => {
  it('acknowledges the nudge by id and removes the banner', async () => {
    const d = doubles();
    await renderOverlay(d);
    await deliver(nudgePayload());

    await fireEvent.press(screen.getByTestId(`nudge-dismiss-${NUDGE_ID}`));

    expect(d.acknowledge).toHaveBeenCalledWith(NUDGE_ID);
    await waitFor(() => expect(screen.queryByTestId(`nudge-${NUDGE_ID}`)).toBeNull());
  });

  it('leaves the banner up when the acknowledge fails', async () => {
    const d = doubles({ acknowledge: status(500) });
    await renderOverlay(d);
    await deliver(nudgePayload());

    await fireEvent.press(screen.getByTestId(`nudge-dismiss-${NUDGE_ID}`));

    await waitFor(() =>
      expect(screen.getByTestId(`nudge-dismiss-failed-${NUDGE_ID}`)).toBeTruthy(),
    );
    expect(screen.getByTestId(`nudge-${NUDGE_ID}`)).toBeTruthy();
  });
});

describe('token freshness', () => {
  it('re-mints before opening when the held token is near exp', async () => {
    const refreshed = freshToken();
    const d = doubles({ remint: ok(session(refreshed)) });
    await renderOverlay(d, staleToken());

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(d.remintToken).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.last.options.headers.Authorization).toBe(`Bearer ${refreshed}`);
  });

  it('does not re-mint when the held token is fresh', async () => {
    const jwt = freshToken();
    const d = doubles();
    await renderOverlay(d, jwt);

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(d.remintToken).not.toHaveBeenCalled();
    expect(FakeWebSocket.last.options.headers.Authorization).toBe(`Bearer ${jwt}`);
  });

  it('re-mints once and retries when the socket fails to open', async () => {
    const refreshed = freshToken();
    const d = doubles({ remint: ok(session(refreshed)) });
    await renderOverlay(d);

    // TASK-041 validates ahead of the handshake, so a refused token arrives as a
    // socket that closes without ever having opened.
    const refused = await openedSocket();
    await act(async () => {
      refused.fireClose();
    });

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    expect(d.remintToken).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.last.options.headers.Authorization).toBe(`Bearer ${refreshed}`);
  });

  it('reports a refusal rather than re-minting a second time', async () => {
    const d = doubles({ remint: ok(session(freshToken())) });
    await renderOverlay(d);

    const first = await openedSocket();
    await act(async () => {
      first.fireClose();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    await act(async () => {
      FakeWebSocket.last.fireClose();
    });

    await waitFor(() => expect(screen.getByTestId('nudge-stream-error')).toBeTruthy());
    expect(d.remintToken).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  /**
   * The regression test for the bug TASK-006b exists to prevent.
   *
   * `POST /sessions/start` used as a refresh forks one visit into two
   * encounters: the transcript splits across two channels, TASK-030 generates
   * two partial SOAP notes, and the procedure dedup stops working. Nothing
   * errors anywhere along that path, so a test is the only thing that can catch
   * it — which is why this asserts the negative directly rather than relying on
   * the re-mint assertions above.
   */
  it('never calls startVisit, on any path', async () => {
    const d = doubles();
    await renderOverlay(d, staleToken());

    await waitFor(() => expect(d.remintToken).toHaveBeenCalled());
    const socket = await openedSocket();
    await act(async () => {
      socket.fireClose();
    });

    expect(d.startVisit).not.toHaveBeenCalled();
  });

  it('treats a 409 from the re-mint as the visit being over and does not retry', async () => {
    const d = doubles({ remint: status(409) });
    await renderOverlay(d, staleToken());

    await waitFor(() => expect(screen.getByTestId('nudge-stream-error')).toBeTruthy());
    expect(FakeWebSocket.instances).toHaveLength(0);
    // The encounter is genuinely finished, so no reconnect is offered.
    expect(screen.queryByTestId('nudge-reconnect')).toBeNull();
    expect(d.remintToken).toHaveBeenCalledTimes(1);
  });

  it('offers a reconnect when the stream drops after opening', async () => {
    const d = doubles();
    await renderOverlay(d);

    const socket = await openedSocket();
    await act(async () => {
      socket.fireOpen();
    });
    await act(async () => {
      socket.fireClose();
    });

    await waitFor(() => expect(screen.getByTestId('nudge-reconnect')).toBeTruthy());
  });
});

describe('the credential', () => {
  it('carries the token in a header and never in the URL', async () => {
    const jwt = freshToken();
    const d = doubles();
    await renderOverlay(d, jwt);

    const socket = await openedSocket();
    expect(socket.url).toBe(`wss://nudges.example/ws/nudges/${SESSION_ID}`);
    expect(socket.url).not.toContain(jwt);
    // React Native has the header carrier, so no subprotocol is offered at all —
    // the browser-only carrier is the one that would put the token in a
    // handshake header the server echoes.
    expect(socket.protocols).toBeUndefined();
  });
});

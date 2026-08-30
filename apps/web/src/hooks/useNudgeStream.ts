/**
 * `useNudgeStream` — one encounter's clinical nudges, live in the browser.
 *
 * The socket is `WebSocket /ws/nudges/{session_id}` on nudge-service (TASK-041),
 * which relays what track-b-rag publishes on `nudges:{session_id}` without
 * parsing it. Validation of the session token happens *before* the handshake
 * completes, so an unauthenticated peer never reaches a state where it could
 * receive a frame — and a rejected token therefore arrives here as a failed
 * upgrade rather than as a close with code 4401.
 *
 * **Token freshness is the substance of this module, not a detail of it.**
 * CLAUDE.md's "Refresh proactively and reactively" rule applies to every new
 * socket, and this is the socket it was written about: it opens later in an
 * encounter than the audio socket does, and `SESSION_TTL_SECONDS` is 15 minutes,
 * so a token close to `exp` is the ordinary case for any visit of real length
 * rather than an edge case. Both halves are implemented:
 *
 * - before opening, a token near `exp` is re-minted and the socket opens with
 *   what came back;
 * - a socket that fails to open re-mints once and retries, rather than reopening
 *   with a token that was just refused.
 *
 * Re-minting is `POST /sessions/{session_id}/token` (TASK-006b) and **never**
 * `POST /sessions/start`, which would create a second encounter for one visit —
 * splitting the transcript, the SOAP note and the nudge dedup, with nothing
 * erroring anywhere along the way. That distinction lives in
 * `@medauth/session-client`, behind two separately named methods, for exactly
 * this reason.
 *
 * At most one refresh per connection attempt, so a token that arrives already
 * near `exp` cannot loop. `retry()` starts a new attempt and clears that guard.
 *
 * Nothing here throws: state is a discriminated union carrying a typed error,
 * per CLAUDE.md's "errors bubble up as typed Result objects". Nothing here logs
 * either — the frames are PHI and the token is a credential.
 */

import { parseNudge, type Nudge } from '@medauth/nudge-client';
import { isNearExpiry, sessionSubprotocols, type ApiFailure } from '@medauth/session-client';
import { useCallback, useEffect, useRef, useState } from 'react';

import { sessionsApi, type SessionsApi } from '../api/sessions';
import { NUDGE_SERVICE_WS_URL } from '../config';

/**
 * Why the stream is not delivering nudges.
 *
 * The distinction reaches a person. `VISIT_COMPLETED` is the only one that means
 * the encounter is genuinely over; the rest leave it open, and a provider whose
 * nudge stream is down needs to know that rather than reading silence as
 * "nothing to flag" — which is precisely what a working stream with no alerts
 * looks like.
 */
export type NudgeStreamErrorCode =
  | 'AUTH_REJECTED'
  | 'VISIT_COMPLETED'
  | 'REFRESH_FAILED'
  | 'STREAM_FAILED';

export interface NudgeStreamError {
  code: NudgeStreamErrorCode;
  message: string;
}

export type NudgeStreamState =
  | { status: 'connecting' }
  | { status: 'open' }
  | { status: 'error'; error: NudgeStreamError };

export interface UseNudgeStreamOptions {
  /** The encounter's session id, from `POST /sessions/start` (TASK-006). */
  sessionId: string;
  /** The session JWT from the same response. Carried as a subprotocol, never logged. */
  jwt: string;
  /** WebSocket origin for nudge-service, e.g. `wss://nudges.example`. */
  baseUrl?: string;
  /** Injected in tests; the default talks to track-a-clinical. */
  sessions?: SessionsApi;
  /** Injected in tests, so "near expiry" does not depend on the wall clock. */
  now?: () => number;
}

export interface NudgeStream {
  state: NudgeStreamState;
  /** Nudges received on this stream, oldest first. */
  nudges: Nudge[];
  /** Drop a nudge from the list once it has been acknowledged. */
  remove: (nudgeId: string) => void;
  /** Start a fresh connection attempt after a failure. */
  retry: () => void;
}

const VISIT_COMPLETED_MESSAGE =
  'This visit has already been completed, so its alerts have stopped. Start a new visit to resume monitoring.';

const REFUSED_MESSAGE =
  'The alert connection was refused and could not be refreshed. Alerts are not being delivered for this visit.';

const DISCONNECTED_MESSAGE =
  'The alert connection dropped. Alerts are not being delivered until it reconnects.';

/**
 * A refused re-mint, turned into what the provider is told.
 *
 * A 409 is the only status that ends the visit: it means the encounter is
 * already completed, so there is nothing left to refresh a token for. Everything
 * else leaves the encounter open and is reported as a failure to connect. The
 * failure's own message is included because it distinguishes unreachable from
 * refused, and neither it nor `ApiFailure` ever carries a token.
 */
function refreshFailure(failure: ApiFailure): NudgeStreamError {
  if (failure.kind === 'status' && failure.status === 409) {
    return { code: 'VISIT_COMPLETED', message: VISIT_COMPLETED_MESSAGE };
  }
  return {
    code: 'REFRESH_FAILED',
    message: `The alert connection could not be refreshed. ${failure.message}`,
  };
}

export function useNudgeStream({
  sessionId,
  jwt,
  baseUrl = NUDGE_SERVICE_WS_URL,
  sessions = sessionsApi,
  now = Date.now,
}: UseNudgeStreamOptions): NudgeStream {
  const identity = `${sessionId} ${jwt}`;

  const [state, setState] = useState<NudgeStreamState>({ status: 'connecting' });
  const [nudges, setNudges] = useState<Nudge[]>([]);

  /**
   * The token this attempt opens with. It starts as the prop and is replaced by
   * a re-mint; `attempt` is what re-enters the effect, because a bumped counter
   * always changes and a replacement token might not.
   */
  const [token, setToken] = useState(jwt);
  const [attempt, setAttempt] = useState(0);

  /** One refresh per attempt, so a token that arrives near `exp` cannot loop. */
  const refreshedRef = useRef(false);

  /** The identity the guard above belongs to. Compared inside the effect. */
  const identityRef = useRef(identity);

  const remove = useCallback((nudgeId: string) => {
    setNudges((current) => current.filter((nudge) => nudge.nudgeId !== nudgeId));
  }, []);

  const retry = useCallback(() => {
    refreshedRef.current = false;
    setState({ status: 'connecting' });
    setAttempt((current) => current + 1);
  }, []);

  /**
   * A new session, or a new token from the caller, is a new stream.
   *
   * Adjusted during render rather than in an effect — React's documented pattern
   * for state that has to follow a prop. An effect would open a socket with the
   * previous encounter's token first and reset immediately afterwards, which is
   * one refused handshake per prop change and, worse, one encounter's alerts
   * briefly attributed to another.
   */
  const [previousIdentity, setPreviousIdentity] = useState(identity);
  if (identity !== previousIdentity) {
    setPreviousIdentity(identity);
    setToken(jwt);
    setNudges([]);
    setState({ status: 'connecting' });
  }

  useEffect(() => {
    if (identityRef.current !== identity) {
      // The refresh guard belongs to one connection sequence, and this is a new
      // one. Reset here rather than in the render block above: a ref must not be
      // written during render, and this effect re-runs on every identity change
      // regardless, since `sessionId` and `token` are both dependencies.
      identityRef.current = identity;
      refreshedRef.current = false;
    }

    let cancelled = false;
    let socket: WebSocket | null = null;
    let opened = false;

    /** Replace the token and re-enter, rather than opening with what we hold. */
    const applyRefreshedToken = (refreshed: string): void => {
      setToken(refreshed);
      setAttempt((current) => current + 1);
    };

    const refresh = async (): Promise<void> => {
      refreshedRef.current = true;
      const refreshed = await sessions.remintToken(sessionId, token);
      if (cancelled) {
        return;
      }
      if (!refreshed.ok) {
        setState({ status: 'error', error: refreshFailure(refreshed.failure) });
        return;
      }
      applyRefreshedToken(refreshed.value.jwt);
    };

    const open = (): void => {
      // The token is a subprotocol value. Never the query string — that is the
      // one place a credential is certain to be written to an intermediary's log.
      socket = new WebSocket(`${baseUrl}/ws/nudges/${sessionId}`, sessionSubprotocols(token));

      socket.onopen = () => {
        opened = true;
        setState({ status: 'open' });
      };

      socket.onmessage = (event: MessageEvent) => {
        if (typeof event.data !== 'string') {
          // The relay sends text frames only. Anything else is not a nudge.
          return;
        }
        const nudge = parseNudge(event.data);
        if (nudge === null) {
          // Dropped without a log line: see `parseNudge`. A half-rendered banner
          // a provider cannot act on is worse than no banner.
          return;
        }
        setNudges((current) =>
          // The emitter raises one nudge per procedure per encounter, but a
          // republish carries the same nudge_id — one banner, not two.
          current.some((existing) => existing.nudgeId === nudge.nudgeId)
            ? current
            : [...current, nudge],
        );
      };

      // There is no `onerror`: a browser fires `error` immediately before
      // `close` on a failed upgrade, so `close` sees every failure, and the
      // error event carries nothing readable — not even a reason — that is not
      // already known here.
      socket.onclose = () => {
        if (cancelled) {
          return;
        }
        if (opened) {
          setState({
            status: 'error',
            error: { code: 'STREAM_FAILED', message: DISCONNECTED_MESSAGE },
          });
          return;
        }
        // Never opened: TASK-041 validates ahead of the handshake, so this is
        // how a refused token arrives. Refresh once and retry.
        if (refreshedRef.current) {
          setState({
            status: 'error',
            error: { code: 'AUTH_REJECTED', message: REFUSED_MESSAGE },
          });
          return;
        }
        void refresh();
      };
    };

    if (!refreshedRef.current && isNearExpiry(token, now())) {
      void refresh();
    } else {
      open();
    }

    return () => {
      cancelled = true;
      if (socket) {
        // Detached before closing, so the teardown's own close event does not
        // run the refused-token branch and report a failure on an unmount.
        socket.onopen = null;
        socket.onmessage = null;
        socket.onclose = null;
        socket.close();
      }
    };
  }, [sessionId, token, attempt, baseUrl, sessions, now, identity]);

  return { state, nudges, remove, retry };
}

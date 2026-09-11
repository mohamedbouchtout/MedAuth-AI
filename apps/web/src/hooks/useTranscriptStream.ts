/**
 * `useTranscriptStream` — one encounter's speech, live in the browser.
 *
 * The socket is `WebSocket /ws/transcript/{session_id}` on nudge-service
 * (TASK-041d), which relays what `audio-ingestion` publishes on
 * `transcription:{session_id}` without parsing it. This is the nudge hook's
 * shape with a different channel and a different parse, which is what TASK-070
 * was told to build rather than a second relay or a second credential path — so
 * the token handling below is deliberately the same and should stay that way.
 *
 * **The distinction this hook exists to preserve is silence.** Redis pub/sub
 * keeps no history: a socket opened late, or reopened after a drop, receives
 * only what is published from that moment, and the accumulated transcript lives
 * in a buffer in another service that no route exposes. So "connected, nothing
 * said yet" and "not connected" both produce an empty list, and a pane that
 * rendered one blank box for both would tell a provider the room is quiet when
 * in fact nothing is being received. `state` and `segments` are returned
 * separately for exactly that reason — a caller must render both, and must not
 * infer connection from whether any speech has arrived.
 *
 * **What this hook must never be read as saying is "this is the transcript".**
 * It is what arrived while this connection was open. A consumer that presents it
 * as a complete record of the encounter is making a claim the bus cannot
 * support, the same way a payer's silence is not a negative determination.
 *
 * **Every frame is PHI** — the words spoken in a clinical encounter, which is
 * the largest disclosure this repository sends to a browser. Nothing here logs a
 * frame, a parse failure, or a token.
 */

import { isNearExpiry, sessionSubprotocols, type ApiFailure } from '@medauth/session-client';
import { useCallback, useEffect, useRef, useState } from 'react';

import { sessionsApi, type SessionsApi } from '../api/sessions';
import { NUDGE_SERVICE_WS_URL } from '../config';
import { parseSegment, type TranscriptSegment } from '../transcript/segment';

/**
 * Why the stream is not delivering speech.
 *
 * The same vocabulary as the nudge stream, and for the same reason: the
 * distinction reaches a person, and `VISIT_COMPLETED` is the only one of them
 * that means the encounter is genuinely over.
 */
export type TranscriptStreamErrorCode =
  | 'AUTH_REJECTED'
  | 'VISIT_COMPLETED'
  | 'REFRESH_FAILED'
  | 'STREAM_FAILED';

export interface TranscriptStreamError {
  code: TranscriptStreamErrorCode;
  message: string;
}

export type TranscriptStreamState =
  | { status: 'connecting' }
  | { status: 'open' }
  | { status: 'error'; error: TranscriptStreamError };

export interface UseTranscriptStreamOptions {
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

export interface TranscriptStream {
  state: TranscriptStreamState;
  /** Segments received on this connection, oldest first. Never a full transcript. */
  segments: TranscriptSegment[];
  /** Start a fresh connection attempt after a failure. */
  retry: () => void;
}

const VISIT_COMPLETED_MESSAGE =
  'This visit has already been completed, so its transcript has stopped.';

const REFUSED_MESSAGE =
  'The transcript connection was refused and could not be refreshed. Speech from this visit is not being shown.';

const DISCONNECTED_MESSAGE =
  'The transcript connection dropped. Anything said while it is down will not appear here, even after it reconnects.';

/**
 * A refused re-mint, turned into what the provider is told.
 *
 * A 409 is the only status that ends the visit. Everything else leaves the
 * encounter open and is reported as a failure to connect — the failure's own
 * message distinguishes unreachable from refused, and neither it nor
 * `ApiFailure` ever carries a token.
 */
function refreshFailure(failure: ApiFailure): TranscriptStreamError {
  if (failure.kind === 'status' && failure.status === 409) {
    return { code: 'VISIT_COMPLETED', message: VISIT_COMPLETED_MESSAGE };
  }
  return {
    code: 'REFRESH_FAILED',
    message: `The transcript connection could not be refreshed. ${failure.message}`,
  };
}

export function useTranscriptStream({
  sessionId,
  jwt,
  baseUrl = NUDGE_SERVICE_WS_URL,
  sessions = sessionsApi,
  now = Date.now,
}: UseTranscriptStreamOptions): TranscriptStream {
  const identity = `${sessionId} ${jwt}`;

  const [state, setState] = useState<TranscriptStreamState>({ status: 'connecting' });
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);

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

  const retry = useCallback(() => {
    refreshedRef.current = false;
    setState({ status: 'connecting' });
    setAttempt((current) => current + 1);
  }, []);

  /**
   * A new session, or a new token from the caller, is a new stream.
   *
   * Adjusted during render rather than in an effect, the same as the nudge hook
   * and for the same reason: an effect would open a socket with the previous
   * encounter's token first, and would briefly show one encounter's speech under
   * another's heading — which on this channel is one patient's words displayed
   * against another patient's visit.
   */
  const [previousIdentity, setPreviousIdentity] = useState(identity);
  if (identity !== previousIdentity) {
    setPreviousIdentity(identity);
    setToken(jwt);
    setSegments([]);
    setState({ status: 'connecting' });
  }

  useEffect(() => {
    if (identityRef.current !== identity) {
      // The refresh guard belongs to one connection sequence, and this is a new
      // one. Reset here rather than in the render block above: a ref must not be
      // written during render.
      identityRef.current = identity;
      refreshedRef.current = false;
    }

    let cancelled = false;
    let socket: WebSocket | null = null;
    let opened = false;

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
      setToken(refreshed.value.jwt);
      setAttempt((current) => current + 1);
    };

    const open = (): void => {
      // The token is a subprotocol value. Never the query string — that is the
      // one place a credential is certain to be written to an intermediary's log.
      socket = new WebSocket(`${baseUrl}/ws/transcript/${sessionId}`, sessionSubprotocols(token));

      socket.onopen = () => {
        opened = true;
        setState({ status: 'open' });
      };

      socket.onmessage = (event: MessageEvent) => {
        if (typeof event.data !== 'string') {
          // The relay sends text frames only. Anything else is not a segment.
          return;
        }
        const segment = parseSegment(event.data);
        if (segment === null) {
          // Dropped without a log line: the frame is PHI, and a partial or
          // unreadable one is not something a provider can act on.
          return;
        }
        setSegments((current) =>
          // Keyed on `result_id`, never on text equality: Transcribe reuses one
          // id across an utterance's revisions, and two utterances can
          // legitimately carry identical words.
          current.some((existing) => existing.resultId === segment.resultId)
            ? current
            : [...current, segment],
        );
      };

      // There is no `onerror`, for the reason the nudge hook gives: a browser
      // fires `error` immediately before `close` on a failed upgrade, and the
      // error event carries nothing readable that is not already known here.
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
        // Never opened: TASK-041d validates ahead of the handshake, so this is
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

  return { state, segments, retry };
}

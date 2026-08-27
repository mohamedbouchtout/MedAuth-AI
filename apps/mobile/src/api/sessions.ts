/**
 * Client for track-a-clinical's session lifecycle endpoints (TASK-006/006b).
 *
 * Three calls, and the distinction between two of them is the whole point:
 *
 * - `POST /sessions/start` **creates** an encounter and is the only call that
 *   may ever be used to begin a visit.
 * - `POST /sessions/{id}/token` **re-mints** a token for a visit that already
 *   exists. CLAUDE.md is explicit that a client which reaches for
 *   `/sessions/start` to get a fresh token forks one visit into two encounters,
 *   silently — the transcript splits, two partial SOAP notes are written, and
 *   procedure dedup stops working. Nothing errors along that path, which is why
 *   the two live behind separately named methods here rather than one
 *   "get a token" helper that could be called in either situation.
 * - `POST /sessions/{id}/end` completes the encounter.
 *
 * Failures are returned as typed results, never thrown, per CLAUDE.md's
 * TypeScript conventions. Nothing in this module logs: the request body carries
 * a patient identifier and every success response carries a session credential.
 */

import { API_BASE_URL } from '../config';

/** A live session: the encounter's id and the token that opens its sockets. */
export interface Session {
  sessionId: string;
  jwt: string;
}

export interface StartVisitInput {
  patientId: string;
  providerId: string;
  ehrEncounterId?: string;
}

/**
 * Why a call did not produce an answer.
 *
 * `status` is kept because callers branch on it — a 409 from the re-mint route
 * means the encounter is already completed and the visit really is over, which
 * is the one case where a provider is asked to start a new one.
 */
export type ApiFailure =
  | { kind: 'network'; message: string }
  | { kind: 'status'; status: number; code: string; message: string }
  | { kind: 'malformed'; message: string };

export type ApiResult<T> = { ok: true; value: T } | { ok: false; failure: ApiFailure };

export interface SessionsApi {
  /** Create an encounter. Never call this to refresh a token — see the module note. */
  startVisit(input: StartVisitInput): Promise<ApiResult<Session>>;
  /** Re-mint for a session that already exists. A 409 means the visit is over. */
  remintToken(sessionId: string, jwt: string): Promise<ApiResult<Session>>;
  endVisit(sessionId: string): Promise<ApiResult<void>>;
}

/** The subset of `fetch` this client uses, so tests can supply their own. */
export type FetchLike = (url: string, init: RequestInit) => Promise<Response>;

interface ErrorBody {
  code: string;
  message: string;
}

const MALFORMED: ApiFailure = {
  kind: 'malformed',
  message: 'The server returned a response MedAuth AI could not read.',
};

function networkFailure(): ApiFailure {
  // The thrown value is not surfaced: it can carry the request URL, and a
  // provider can act on "unreachable" but not on a stack trace.
  return {
    kind: 'network',
    message: 'MedAuth AI could not reach the server. Check the network connection.',
  };
}

function readError(body: unknown, status: number): ApiFailure {
  const error = (body as { error?: unknown } | null)?.error;
  if (typeof error === 'object' && error !== null) {
    const { code, message } = error as Partial<ErrorBody>;
    if (typeof code === 'string' && typeof message === 'string') {
      return { kind: 'status', status, code, message };
    }
  }
  return { kind: 'status', status, code: 'unknown', message: `The server returned ${status}.` };
}

function readSession(body: unknown): Session | null {
  const data = (body as { data?: unknown } | null)?.data;
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  const { session_id: sessionId, jwt } = data as { session_id?: unknown; jwt?: unknown };
  if (typeof sessionId !== 'string' || typeof jwt !== 'string') {
    return null;
  }
  return { sessionId, jwt };
}

export function createSessionsApi(
  baseUrl: string = API_BASE_URL,
  fetchImpl: FetchLike = (url, init) => fetch(url, init),
): SessionsApi {
  async function call(path: string, init: RequestInit): Promise<ApiResult<unknown>> {
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, init);
    } catch {
      return { ok: false, failure: networkFailure() };
    }

    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // A body that will not parse is only fatal on the success path — an error
      // status still tells the caller what happened without one.
      if (response.ok) {
        return { ok: false, failure: MALFORMED };
      }
    }

    if (!response.ok) {
      return { ok: false, failure: readError(body, response.status) };
    }
    return { ok: true, value: body };
  }

  function jsonPost(payload?: Record<string, unknown>, jwt?: string): RequestInit {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (jwt !== undefined) {
      // The session's own token is the re-mint credential, expired or not, in an
      // Authorization header — never the query string, per CLAUDE.md.
      headers.Authorization = `Bearer ${jwt}`;
    }
    return payload === undefined
      ? { method: 'POST', headers }
      : { method: 'POST', headers, body: JSON.stringify(payload) };
  }

  async function sessionCall(path: string, init: RequestInit): Promise<ApiResult<Session>> {
    const result = await call(path, init);
    if (!result.ok) {
      return result;
    }
    const session = readSession(result.value);
    return session === null ? { ok: false, failure: MALFORMED } : { ok: true, value: session };
  }

  return {
    startVisit(input) {
      const payload: Record<string, unknown> = {
        patient_id: input.patientId,
        provider_id: input.providerId,
      };
      if (input.ehrEncounterId !== undefined) {
        payload.ehr_encounter_id = input.ehrEncounterId;
      }
      return sessionCall('/sessions/start', jsonPost(payload));
    },

    remintToken(sessionId, jwt) {
      return sessionCall(`/sessions/${sessionId}/token`, jsonPost(undefined, jwt));
    },

    async endVisit(sessionId) {
      const result = await call(`/sessions/${sessionId}/end`, jsonPost());
      return result.ok ? { ok: true, value: undefined } : result;
    },
  };
}

/** The client the screen uses when none is injected. */
export const sessionsApi: SessionsApi = createSessionsApi();

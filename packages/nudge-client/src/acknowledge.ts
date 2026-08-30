/**
 * Client for track-b-rag's nudge acknowledge route (TASK-041b).
 *
 * **Why this is a package.** It was `apps/web/src/api/nudges.ts` until TASK-043,
 * when `apps/mobile`'s nudge banner became the second client dismissing a nudge.
 * That is the trigger this repository extracts on rather than copies — the same
 * one that produced `packages/api-envelope`, `packages/session-auth`,
 * `packages/cors-policy` and `packages/session-client`. The hazard here is the
 * body and the missing credential: both are decisions made once, in CLAUDE.md,
 * and a second hand-maintained copy is how one app comes to send an empty PATCH
 * or to attach a session token that the route does not read.
 *
 * **Each app binds its own base URL and nothing else.** `createNudgesApi` takes
 * the origin because the two apps configure it differently —
 * `VITE_TRACK_B_RAG_URL` on web, `EXPO_PUBLIC_TRACK_B_RAG_URL` on mobile — and a
 * default reaching into either app's config is what would make this package
 * depend on its consumers.
 *
 * **This call carries no credential, and that is the specified behaviour rather
 * than an omission.** `PATCH /nudges/{nudge_id}/acknowledge` does not validate a
 * session token: its path names a nudge, not a session, so
 * `packages/session-auth`'s check — the token's `session_id` claim against the
 * path's — has nothing to compare against. CLAUDE.md settles this under "A route
 * keyed on a resource rather than a session follows the same v1 rule", where it
 * is decided once for this app and `apps/mobile` together. Attaching the session
 * JWT here anyway would be worse than useless: the service ignores it, and a
 * client that sends one comes to believe it is authenticated when it is not.
 *
 * **The body is `{"acknowledged": true}`, not an empty PATCH**, and `false` is
 * rejected by the service rather than read as an un-acknowledge.
 *
 * The result types come from `@medauth/session-client` so this app has one
 * vocabulary for "a call did not produce an answer". The envelope reading is
 * local: that package is the session lifecycle client by its own scope note, not
 * a general HTTP client, and this is one route belonging to another service. A
 * third consumer of the envelope in TypeScript is what would justify a package
 * for it, mirroring `packages/api-envelope` on the Python side.
 *
 * Nothing here logs. A nudge names a procedure and an encounter's undocumented
 * criteria, and a failure message can carry the request URL.
 */

import type { ApiFailure, ApiResult } from '@medauth/session-client';

/** What the service reports about a dismissal. */
export interface Acknowledgement {
  nudgeId: string;
  acknowledgedAt: string;
  /**
   * Whether this call was a no-op repeat. The route is idempotent — a second
   * dismissal is a 200 carrying the *original* timestamp, not an error — so a
   * client that dismisses twice has still succeeded both times.
   */
  alreadyAcknowledged: boolean;
}

/** The subset of `fetch` this client uses, so tests can supply their own. */
export type FetchLike = (url: string, init: RequestInit) => Promise<Response>;

export interface NudgesApi {
  acknowledge(nudgeId: string): Promise<ApiResult<Acknowledgement>>;
}

const MALFORMED: ApiFailure = {
  kind: 'malformed',
  message: 'The server returned a response MedAuth AI could not read.',
};

function networkFailure(): ApiFailure {
  return {
    kind: 'network',
    message: 'MedAuth AI could not reach the server. Check the network connection.',
  };
}

function readError(body: unknown, status: number): ApiFailure {
  const error = (body as { error?: unknown } | null)?.error;
  if (typeof error === 'object' && error !== null) {
    const { code, message } = error as { code?: unknown; message?: unknown };
    if (typeof code === 'string' && typeof message === 'string') {
      return { kind: 'status', status, code, message };
    }
  }
  return { kind: 'status', status, code: 'unknown', message: `The server returned ${status}.` };
}

function readAcknowledgement(body: unknown): Acknowledgement | null {
  const data = (body as { data?: unknown } | null)?.data;
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  const {
    nudge_id: nudgeId,
    acknowledged_at: acknowledgedAt,
    already_acknowledged: alreadyAcknowledged,
  } = data as {
    nudge_id?: unknown;
    acknowledged_at?: unknown;
    already_acknowledged?: unknown;
  };
  if (
    typeof nudgeId !== 'string' ||
    typeof acknowledgedAt !== 'string' ||
    typeof alreadyAcknowledged !== 'boolean'
  ) {
    return null;
  }
  return { nudgeId, acknowledgedAt, alreadyAcknowledged };
}

export function createNudgesApi(
  baseUrl: string,
  fetchImpl: FetchLike = (url, init) => fetch(url, init),
): NudgesApi {
  return {
    async acknowledge(nudgeId) {
      let response: Response;
      try {
        response = await fetchImpl(`${baseUrl}/nudges/${nudgeId}/acknowledge`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ acknowledged: true }),
        });
      } catch {
        // The thrown value is not surfaced: it can carry the request URL, and a
        // provider can act on "unreachable" but not on a stack trace.
        return { ok: false, failure: networkFailure() };
      }

      let body: unknown = null;
      try {
        body = await response.json();
      } catch {
        // A body that will not parse is only fatal on the success path — an
        // error status still says what happened without one.
        if (response.ok) {
          return { ok: false, failure: MALFORMED };
        }
      }

      if (!response.ok) {
        return { ok: false, failure: readError(body, response.status) };
      }

      const acknowledgement = readAcknowledgement(body);
      return acknowledgement === null
        ? { ok: false, failure: MALFORMED }
        : { ok: true, value: acknowledgement };
    },
  };
}

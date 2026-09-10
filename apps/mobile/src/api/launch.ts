/**
 * Client for the two routes that obtain a SMART launch (TASK-025c).
 *
 * `GET /fhir/launch` starts one — the browser goes there and the EHR takes over
 * — and `POST /fhir/launch/claim` (TASK-051f) collects the result afterwards.
 * They are one flow rather than two calls: the first is a URL this app opens
 * and never fetches, the second is the only fetch in the exchange.
 *
 * **`delivery=mobile` is what makes the flow reachable at all.** Without it the
 * callback answers `{launch_id, ehr_type, expires_in}` as JSON in whatever
 * browser the EHR redirected, which is a correct answer for a
 * service-to-service caller and no answer at all for an app — the browser
 * renders the JSON and this app never sees it. With it, the callback redirects
 * to `SMART_MOBILE_RETURN_URI` carrying a single-use claim code, and that is
 * what `redeemClaim` spends. See CLAUDE.md, "Handing a completed SMART launch
 * back to a client".
 *
 * **The claim code is not the `launch_id`.** It names one launch for about two
 * minutes and dies on first use, which is what makes it safe to carry in a
 * redirect where a `launch_id` — a handle that resolves to an EHR access token
 * — is not. Nothing here logs either of them.
 *
 * Failures come back as typed results and are never thrown, per CLAUDE.md's
 * TypeScript conventions, on the same terms as `./fhir`.
 */

import type { ApiFailure, ApiResult, FetchLike } from '@medauth/session-client';

/**
 * The delivery this app declares, fixed rather than a parameter.
 *
 * A client asking for any other delivery would be asking for an answer it
 * cannot read: `json` goes to the browser, and `web` redirects to the web app's
 * return URL, which this platform does not own.
 */
export const MOBILE_DELIVERY = 'mobile';

/** What the app asks the EHR for. */
export interface LaunchRequest {
  /**
   * The EHR's FHIR base URL.
   *
   * Supplied from configuration for a standalone launch and by the EHR itself
   * for an EHR launch — see `../config` for why one configured issuer is a
   * scope limit rather than a design.
   */
  iss: string;
  /**
   * The EHR's opaque launch context.
   *
   * Present on an EHR launch and absent on a standalone one. Its presence is
   * the *only* thing that distinguishes the two, and which one happened decides
   * whether the launch yields a patient — so it is never defaulted or invented.
   */
  launch?: string;
}

/** A launch this app now holds. Exactly what the service hands back. */
export interface LaunchSession {
  /**
   * Names the launch and the EHR access token behind it.
   *
   * A credential by this repository's own definition, so it is held in memory
   * for the life of the process and never written to disk or to a log line.
   * It is not a `session_id`: a launch precedes the visit and outlives several
   * of them.
   */
  launchId: string;
  /** The vendor the issuer resolved to, or `generic`. Operational detail. */
  ehrType: string;
  /** Seconds until the EHR access token behind the launch expires. */
  expiresInSeconds: number;
}

export interface LaunchApi {
  /** The URL to open in the system browser to begin a launch. */
  authorizeUrl(request: LaunchRequest): string;
  /** Exchange the redirect's claim code for the launch it names. */
  redeemClaim(claim: string): Promise<ApiResult<LaunchSession>>;
}

const MALFORMED: ApiFailure = {
  kind: 'malformed',
  message: 'The server returned a response MedAuth AI could not read.',
};

function networkFailure(): ApiFailure {
  // The thrown value is not surfaced: it can name the request URL, and this
  // request's body is a live credential.
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

function readSession(body: unknown): LaunchSession | null {
  const data = (body as { data?: unknown } | null)?.data;
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  const record = data as Record<string, unknown>;
  if (typeof record.launch_id !== 'string' || record.launch_id === '') {
    return null;
  }
  return {
    launchId: record.launch_id,
    ehrType: typeof record.ehr_type === 'string' ? record.ehr_type : 'generic',
    // A launch whose expiry did not survive the wire is still a usable launch:
    // the service refreshes the EHR token on its own and this number is
    // reported, not acted on.
    expiresInSeconds: typeof record.expires_in === 'number' ? record.expires_in : 0,
  };
}

export function createLaunchApi(
  baseUrl: string,
  fetchImpl: FetchLike = (url, init) => fetch(url, init),
): LaunchApi {
  return {
    authorizeUrl({ iss, launch }) {
      const parameters = new URLSearchParams({ iss, delivery: MOBILE_DELIVERY });
      if (launch !== undefined && launch !== '') {
        parameters.set('launch', launch);
      }
      return `${baseUrl}/fhir/launch?${parameters.toString()}`;
    },

    async redeemClaim(claim) {
      let response: Response;
      try {
        // A POST, and the code travels in the body. Redeeming through a URL
        // would give back exactly what carrying a claim code instead of a
        // launch_id was for.
        response = await fetchImpl(`${baseUrl}/fhir/launch/claim`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ claim }),
        });
      } catch {
        return { ok: false, failure: networkFailure() };
      }

      let body: unknown = null;
      try {
        body = await response.json();
      } catch {
        if (response.ok) {
          return { ok: false, failure: MALFORMED };
        }
      }

      if (!response.ok) {
        return { ok: false, failure: readError(body, response.status) };
      }
      const session = readSession(body);
      return session === null ? { ok: false, failure: MALFORMED } : { ok: true, value: session };
    },
  };
}

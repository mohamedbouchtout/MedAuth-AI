/**
 * Client for the two routes that obtain a SMART launch.
 *
 * `GET /fhir/launch` starts one — the browser goes there and the EHR takes over
 * — and `POST /fhir/launch/claim` (TASK-051f) collects the result afterwards.
 * They are one flow rather than two calls: the first is a URL an app opens and
 * never fetches, the second is the only fetch in the exchange.
 *
 * **The `delivery` parameter is what makes the flow reachable at all.** Without
 * it the callback answers `{launch_id, ehr_type, expires_in}` as JSON in
 * whatever browser the EHR redirected, which is a correct answer for a
 * service-to-service caller and no answer at all for an app — the browser
 * renders the JSON and the app that started the launch never sees it. With it,
 * the callback redirects to that platform's configured return target carrying a
 * single-use claim code, and that is what `redeemClaim` spends. See CLAUDE.md,
 * "Handing a completed SMART launch back to a client".
 *
 * **The claim code is not the `launch_id`.** It names one launch for about two
 * minutes and dies on first use, which is what makes it safe to carry in a
 * redirect where a `launch_id` — a handle that resolves to an EHR access token
 * — is not. Nothing here logs either of them.
 *
 * Failures come back as typed results and are never thrown, per CLAUDE.md's
 * TypeScript conventions, on the same terms as `./fhir`.
 */

import type { ApiResult, FetchLike } from '@medauth/session-client';

import { MALFORMED, networkFailure, readEnvelope } from './http';

/**
 * How a completed launch is handed back — the client-reachable half of the
 * service's `LaunchDelivery`.
 *
 * A closed vocabulary rather than a string, for the third reason this
 * repository has made one (after `payer-vocab`'s slugs and `EHRType`): the
 * value is matched by equality on the far side, and it round-trips — the
 * service records it on `fhir_launch:{state}` at launch and reads it back at
 * the callback, so a free-form string would put the write side and the read
 * side in two modules with nothing holding them in step.
 *
 * `json` is deliberately absent although the service accepts it. It is the
 * answer for a service-to-service caller, and an app asking for it would be
 * asking for an answer it cannot read: the JSON renders in the browser and the
 * app never sees it. That is the failure TASK-051f exists to close, so the type
 * refuses to express it.
 */
export type LaunchDelivery = 'web' | 'mobile';

/** What the app asks the EHR for. */
export interface LaunchRequest {
  /**
   * The EHR's FHIR base URL.
   *
   * Supplied from configuration for a standalone launch and by the EHR itself
   * for an EHR launch — see each app's `config` for why one configured issuer
   * is a scope limit rather than a design.
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

/** A launch an app now holds. Exactly what the service hands back. */
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
  /** The URL to send the browser to in order to begin a launch. */
  authorizeUrl(request: LaunchRequest): string;
  /** Exchange the redirect's claim code for the launch it names. */
  redeemClaim(claim: string): Promise<ApiResult<LaunchSession>>;
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

/**
 * Build the launch client for one platform.
 *
 * `delivery` is fixed per app rather than per call: which return target a
 * completed launch is handed back through is a property of the platform the app
 * is running on, and a caller choosing it per launch could only choose wrong.
 */
export function createLaunchApi(
  baseUrl: string,
  delivery: LaunchDelivery,
  fetchImpl: FetchLike = (url, init) => fetch(url, init),
): LaunchApi {
  return {
    authorizeUrl({ iss, launch }) {
      const parameters = new URLSearchParams({ iss, delivery });
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

      const result = await readEnvelope(response);
      if (!result.ok) {
        return result;
      }
      const session = readSession(result.value);
      return session === null ? { ok: false, failure: MALFORMED } : { ok: true, value: session };
    },
  };
}

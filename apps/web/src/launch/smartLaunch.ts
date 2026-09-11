/**
 * Performing a SMART launch from this app (TASK-070).
 *
 * Both routes that name a patient are keyed on a `launch_id`, because both spend
 * its EHR access token. This is the piece that obtains one, and it is in two
 * halves rather than one function — which is the whole structural difference
 * from `apps/mobile`'s `smartLaunch.ts`, where the exchange is a single
 * awaitable call.
 *
 * **A browser launch is not a call, it is a departure and a return.** Navigating
 * to the authorize URL unloads this app entirely: no promise resolves, no
 * `finally` runs, and nothing in this module is alive while the provider signs
 * in. The EHR eventually redirects to `SMART_WEB_RETURN_URL` and this app boots
 * again, from nothing, with a claim code in its URL. So `beginLaunch` returns by
 * not returning, and `completeLaunch` runs in a different page's lifetime.
 *
 * Two consequences a later reader should not have to rediscover:
 *
 * - **Nothing may be held in memory across the two halves.** Anything the second
 *   half needs has to be in the URL the EHR redirects to or in configuration.
 *   That is exactly why the handoff carries a claim code: it is the only channel
 *   there is, and a `launch_id` must never travel that way.
 * - **A provider who abandons the login never comes back here at all.** There is
 *   no cancellation to observe, unlike mobile's `openAuthSessionAsync`, which
 *   reports one. A launch that *reaches* the EHR and fails there is a different
 *   case and is reported: TASK-051g redirects it back here carrying an `error`,
 *   which `messageForFailure` turns into what the provider is told.
 *
 * Nothing here logs. The authorize URL carries the EHR's launch context and the
 * claim code is a live credential.
 */

import type { LaunchApi, LaunchRequest, LaunchSession } from '../api/fhirClient';
import type { LaunchFailure } from './inbound';

/** Navigates the current page. Injected so a test can observe it without a DOM. */
export type Navigate = (url: string) => void;

/**
 * The result of redeeming a claim code.
 *
 * A launch that could not be redeemed is a failure with a message, never a
 * silent fall-through to the standalone path: the provider believed they signed
 * in to the EHR, and quietly offering a patient search instead would hide that
 * the chart they launched from is not the one MedAuth is looking at.
 */
export type LaunchOutcome =
  | { kind: 'launched'; session: LaunchSession }
  | { kind: 'failed'; message: string };

export const REDEMPTION_FAILED_MESSAGE =
  'MedAuth AI could not complete the EHR sign-in. Start the launch again from the chart.';

export const DECLINED_MESSAGE =
  'The EHR did not allow the sign-in. Sign in again, or ask whoever administers the EHR whether MedAuth AI is permitted for your account.';

export const LAUNCH_FAILED_MESSAGE =
  'The EHR sign-in did not complete. Sign in again.';

/**
 * What a provider is told about a launch that came back having failed.
 *
 * **Two messages because there are two members, and no more than two.** A
 * provider who declined at the EHR can act on that differently from one whose
 * sign-in broke; nothing finer would change what they do next, and the service
 * deliberately sends nothing finer. The EHR's own refusal reason is not here
 * because it never leaves the service — a third party's string rendered in this
 * UI is read as ours. See CLAUDE.md, "A failed launch is delivered the same way,
 * and carries no claim code".
 *
 * Exhaustive over `LaunchFailure`, so a member added on the service side fails
 * typechecking here rather than rendering as nothing.
 */
export function messageForFailure(failure: LaunchFailure): string {
  switch (failure) {
    case 'declined':
      return DECLINED_MESSAGE;
    case 'failed':
      return LAUNCH_FAILED_MESSAGE;
    default: {
      const unreachable: never = failure;
      return unreachable;
    }
  }
}

/**
 * Send the browser to the EHR's authorization endpoint.
 *
 * Returns nothing because there is nothing to return: by the time the navigation
 * settles this page no longer exists. A caller must not await anything after
 * this or render a "launching…" state it expects to replace itself.
 */
export function beginLaunch(api: LaunchApi, request: LaunchRequest, navigate: Navigate): void {
  navigate(api.authorizeUrl(request));
}

/**
 * Spend a claim code for the launch it names.
 *
 * Redeemed immediately on the load that found it, and never stored first: the
 * code is single-use and short-lived, and holding it is holding a credential for
 * no benefit.
 *
 * A failed redemption is reported with fixed text rather than the service's own
 * message. Unknown, expired and already-redeemed are deliberately one answer on
 * that route — so a caller probing codes learns nothing — and there is nothing a
 * provider can do differently about any of them beyond launching again.
 */
export async function completeLaunch(api: LaunchApi, claim: string): Promise<LaunchOutcome> {
  const result = await api.redeemClaim(claim);
  return result.ok
    ? { kind: 'launched', session: result.value }
    : { kind: 'failed', message: REDEMPTION_FAILED_MESSAGE };
}

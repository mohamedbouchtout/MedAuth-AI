/**
 * Recognising a completed SMART launch in this app's own URL (TASK-070).
 *
 * This is the web half of TASK-051f's handoff. `apps/mobile` gets its redirect
 * back through `openAuthSessionAsync`, which hands the app a URL and never
 * navigates anything; a browser has no such mechanism. The page that started the
 * launch is gone — it navigated to the EHR — and what comes back is a fresh load
 * of this app at `SMART_WEB_RETURN_URL` with `?claim=...` appended. So the
 * inbound launch is not an event this app receives, it is a fact about the URL
 * it booted at.
 *
 * **The claim code is what identifies a launch return, not the path.** The
 * service owns where it redirects, through its own `SMART_WEB_RETURN_URL`, and
 * this app cannot enforce that setting from here. Matching on a path would mean
 * holding a second copy of that value and discarding a completed launch whenever
 * the two disagreed by one character — after a human has logged in and a real
 * credential has been spent, which is the single worst place in this system to
 * drop something. A `claim` parameter is unambiguous wherever it lands.
 *
 * **A claim code is a credential**, short-lived and single-use. Nothing here
 * logs it, and `scrubLaunchParams` takes it out of the address bar and out of
 * session history the moment it has been read — a spent code in a URL is a
 * credential sitting in a place browsers are built to remember.
 *
 * **A launch can also come back having failed** (TASK-051g). Until that existed
 * a declined launch rendered the service's own JSON error document in this tab
 * and this app never ran at all; now it redirects here carrying an `error`
 * instead of a `claim`, and the two are the only things that distinguish a
 * finished launch from a plain load. Reading it is what lets this app say "the
 * sign-in did not complete" rather than showing the sign-in screen again as
 * though nothing had happened.
 */

/** The query parameter TASK-051f's callback appends to the return target. */
import { LAUNCH_ERROR_PARAM, narrowLaunchFailure, type LaunchFailure } from '@medauth/fhir-client';

export const CLAIM_PARAM = 'claim';

/**
 * The query parameter a failed launch arrives on, and the vocabulary it carries.
 *
 * Both come from `@medauth/fhir-client` rather than being declared here, because
 * `apps/mobile` reads the same redirect and narrows the same values — and the
 * interesting case is what an *unrecognised* value means. Two apps deciding that
 * separately is how one of them ends up showing a sign-in screen to a provider
 * the EHR has just refused. What stays local is the URL parsing: this app reads
 * its own `location.search`, mobile hand-parses a custom-scheme URI.
 */
export { LAUNCH_ERROR_PARAM as ERROR_PARAM } from '@medauth/fhir-client';
export type { LaunchFailure } from '@medauth/fhir-client';

/**
 * Read the claim code out of a URL's query string, or null when there is none.
 *
 * Null is the ordinary case, not an error: it is what every load of this app
 * that did not follow a launch looks like.
 */
export function readClaim(search: string): string | null {
  const claim = new URLSearchParams(search).get(CLAIM_PARAM);
  // An empty `?claim=` names no launch. Treated as absent rather than redeemed,
  // which would spend a request to be told 404 by a route that is right to
  // answer that way.
  return claim === null || claim === '' ? null : claim;
}

/**
 * Read why a launch failed out of this app's own query string, or null.
 *
 * The narrowing — including what an unrecognised value means — belongs to
 * `narrowLaunchFailure`; this is the browser's half of it. Null is the ordinary
 * case: every load that did not follow a failed launch.
 */
export function readLaunchFailure(search: string): LaunchFailure | null {
  return narrowLaunchFailure(new URLSearchParams(search).get(LAUNCH_ERROR_PARAM));
}

/**
 * Take the launch parameters out of the current URL without reloading.
 *
 * `replaceState` rather than `pushState`: the launch return is not a place the
 * provider should be able to go back to. Returning to it would re-run the app
 * against a code that has already been redeemed, which is a 404 by design and
 * would present as a failed launch rather than as what it is — and, for a
 * failure, would re-report a launch the provider has already been told about.
 *
 * **Both parameters go, for two different reasons.** A claim code is a
 * credential and the address bar is a place browsers remember. An `error` is not
 * a credential; it is removed because it is a report of one moment, and leaving
 * it in the URL would make a reload replay a failure that is over.
 *
 * Every other query parameter and the path are left exactly as they were.
 */
export function scrubLaunchParams(history: History, location: Location): void {
  const parameters = new URLSearchParams(location.search);
  if (!parameters.has(CLAIM_PARAM) && !parameters.has(LAUNCH_ERROR_PARAM)) {
    return;
  }
  parameters.delete(CLAIM_PARAM);
  parameters.delete(LAUNCH_ERROR_PARAM);
  const query = parameters.toString();
  history.replaceState(null, '', `${location.pathname}${query === '' ? '' : `?${query}`}`);
}

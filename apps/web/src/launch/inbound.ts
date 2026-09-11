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
 * logs it, and `scrubClaim` takes it out of the address bar and out of session
 * history the moment it has been read — a spent code in a URL is a credential
 * sitting in a place browsers are built to remember.
 */

/** The query parameter TASK-051f's callback appends to the return target. */
export const CLAIM_PARAM = 'claim';

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
 * Take the claim code out of the current URL without reloading or navigating.
 *
 * `replaceState` rather than `pushState`: the launch return is not a place the
 * provider should be able to go back to. Returning to it would re-run the app
 * against a code that has already been redeemed, which is a 404 by design and
 * would present as a failed launch rather than as what it is.
 *
 * Every other query parameter and the path are left exactly as they were — this
 * removes a credential, it does not navigate.
 */
export function scrubClaim(history: History, location: Location): void {
  const parameters = new URLSearchParams(location.search);
  if (!parameters.has(CLAIM_PARAM)) {
    return;
  }
  parameters.delete(CLAIM_PARAM);
  const query = parameters.toString();
  history.replaceState(null, '', `${location.pathname}${query === '' ? '' : `?${query}`}`);
}

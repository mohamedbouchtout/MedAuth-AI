/**
 * Performing a SMART launch from this app (TASK-025c).
 *
 * TASK-025b filled everything downstream of a `launch_id` and left the launch
 * itself missing, so this app could identify nobody: both routes that name a
 * patient are keyed on a launch because both spend its EHR access token. This
 * is the piece that obtains one.
 *
 * **The whole exchange, in one function**, because its steps are only
 * meaningful together: open the authorize URL in a system browser, wait for the
 * OS to route the redirect back, read the claim code off it, and spend the code
 * immediately. A caller gets one of three outcomes and no intermediate state —
 * a half-finished launch is the thing this app must never hold.
 *
 * **A system browser, never an in-app `WebView`.** The provider types an EHR
 * password into that window, and a `WebView` is a screen this app can read.
 * `openAuthSessionAsync` is also what returns the redirect to the app that
 * opened it, which is what keeps the claim code's window narrow rather than
 * open.
 *
 * **Nothing here is persisted and nothing here is logged.** The claim code is a
 * short-lived credential and the `launch_id` it buys is a longer-lived one; a
 * credential in a log line is a credential in a log line either way.
 */

import {
  LAUNCH_ERROR_PARAM,
  narrowLaunchFailure,
  type LaunchApi,
  type LaunchFailure,
  type LaunchRequest,
  type LaunchSession,
} from '@medauth/fhir-client';

import { queryParam } from './uri';

/**
 * What `WebBrowser.openAuthSessionAsync` answers, structurally.
 *
 * Declared here rather than imported so the orchestration can be driven in a
 * test without the native module, and so the one field that matters — the
 * redirect URL, present only on success — is named in this file's own terms.
 */
export interface AuthSessionResult {
  type: string;
  url?: string;
}

/** Opens `url` in a system browser and resolves when `returnUri` is reached. */
export type AuthSessionOpener = (url: string, returnUri: string) => Promise<AuthSessionResult>;

/**
 * The result of a launch attempt.
 *
 * Cancellation is its own outcome rather than a failure with a message, because
 * it is not an error: a provider who closes the login window has decided not to
 * start a visit, and telling them something went wrong would be false. The
 * screen renders the two differently for that reason.
 */
export type LaunchOutcome =
  | { kind: 'launched'; session: LaunchSession }
  | { kind: 'cancelled' }
  | { kind: 'failed'; message: string };

export const BROWSER_UNAVAILABLE_MESSAGE =
  'MedAuth AI could not open a browser to sign in to the EHR.';

/**
 * What the app says when the redirect arrived carrying neither a claim code nor
 * a reported failure.
 *
 * The likeliest cause is not the server: it is a `SMART_RETURN_URI` this build
 * does not agree with fhir-integration about, or a scheme `app.json` does not
 * register. Both are configuration, and neither is anything a provider can act
 * on, so the message says who to tell. A redirect that *does* report a failure
 * is a different thing and must not land here — see `performSmartLaunch`.
 */
export const NO_CLAIM_MESSAGE =
  'The EHR sign-in finished but MedAuth AI did not receive the launch. Report this to your administrator: the app and the server may disagree about where sign-in returns to.';

export const REDEEM_FAILED_PREFIX = 'MedAuth AI could not complete the EHR launch.';

export const DECLINED_MESSAGE =
  'The EHR did not allow the sign-in. Sign in again, or ask whoever administers the EHR whether MedAuth AI is permitted for your account.';

export const LAUNCH_FAILED_MESSAGE = 'The EHR sign-in did not complete. Sign in again.';

/**
 * What the provider is told about a launch the service reported as failed.
 *
 * Exhaustive over `LaunchFailure`, so a member added on the service side fails
 * typechecking here rather than rendering as nothing. The wording is this app's
 * and the vocabulary is the package's — the same split `apps/web` makes, because
 * the copy is UI and `@medauth/fhir-client` holds none.
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
 * Read a reported failure off the redirect this app was returned to.
 *
 * Null when the redirect reported none, which is every completed launch.
 */
export function failureFromRedirect(url: string): LaunchFailure | null {
  return narrowLaunchFailure(queryParam(url, LAUNCH_ERROR_PARAM));
}

/**
 * The query parameter the claim code arrives on.
 *
 * Fixed by `CLAIM_QUERY_PARAM` in fhir-integration's `smart/delivery.py`. It is
 * the code and never the `launch_id`, which is a capability handle and never
 * goes in a URL.
 */
export const CLAIM_PARAM = 'claim';

/**
 * Read the claim code off the redirect this app was returned to.
 *
 * Returns null when the redirect carried no usable code, which the caller
 * reports rather than retries: a missing code is a configuration mismatch, not a
 * transient failure. See `./uri` for why the query is parsed by hand.
 */
export function claimFromRedirect(url: string): string | null {
  return queryParam(url, CLAIM_PARAM);
}

export interface PerformLaunchInput {
  /** Which EHR, and whether the EHR itself started this. */
  request: LaunchRequest;
  /** This app's return target — must match the service's own copy. */
  returnUri: string;
  api: LaunchApi;
  open: AuthSessionOpener;
}

/**
 * Run one launch attempt to a decided outcome.
 *
 * A cancelled or failed attempt leaves this app holding nothing at all, which is
 * the point: a partially-configured launch would let the patient routes be
 * called with a handle naming no EHR credential, and the failure would surface
 * as a 404 from a route rather than as a launch that did not happen.
 */
export async function performSmartLaunch({
  request,
  returnUri,
  api,
  open,
}: PerformLaunchInput): Promise<LaunchOutcome> {
  let result: AuthSessionResult;
  try {
    result = await open(api.authorizeUrl(request), returnUri);
  } catch {
    // The thrown value is not surfaced: it can carry the authorize URL, which
    // names the EHR and the launch context the EHR gave us.
    return { kind: 'failed', message: BROWSER_UNAVAILABLE_MESSAGE };
  }

  // `cancel` is the provider closing the window; `dismiss` is the app being
  // brought forward some other way. Neither is an error, and both leave this app
  // exactly as it was.
  if (result.type === 'cancel' || result.type === 'dismiss') {
    return { kind: 'cancelled' };
  }
  if (result.type !== 'success' || result.url === undefined) {
    // `locked` — another auth session is already open — lands here. It is a real
    // failure rather than a cancellation, because the provider decided nothing.
    return { kind: 'failed', message: BROWSER_UNAVAILABLE_MESSAGE };
  }

  const claim = claimFromRedirect(result.url);
  if (claim === null) {
    // **A redirect with no claim is not automatically a misconfiguration any
    // more** (TASK-051g). The service now delivers a *failed* launch back
    // through this same return target, carrying an error instead of a code, so
    // checking for one before reaching `NO_CLAIM_MESSAGE` is what stops a
    // provider the EHR refused from being told the app and the server disagree
    // about where sign-in returns to. Before that delivery existed a failure
    // never reached this app at all: the callback rendered JSON in the system
    // browser, no redirect arrived, and closing the window read as a cancel.
    const failure = failureFromRedirect(result.url);
    if (failure !== null) {
      return { kind: 'failed', message: messageForFailure(failure) };
    }
    return { kind: 'failed', message: NO_CLAIM_MESSAGE };
  }

  // Redeemed immediately and never held: the code is single-use and lives about
  // two minutes, so there is no state worth keeping and every reason not to keep
  // it.
  const redeemed = await api.redeemClaim(claim);
  if (!redeemed.ok) {
    return { kind: 'failed', message: `${REDEEM_FAILED_PREFIX} ${redeemed.failure.message}` };
  }
  return { kind: 'launched', session: redeemed.value };
}

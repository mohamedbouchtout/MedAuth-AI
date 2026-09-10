import type { ApiResult } from '@medauth/session-client';

import type { LaunchApi, LaunchSession } from '../../../src/api/launch';
import {
  BROWSER_UNAVAILABLE_MESSAGE,
  NO_CLAIM_MESSAGE,
  performSmartLaunch,
  type AuthSessionOpener,
  type AuthSessionResult,
} from '../../../src/launch/smartLaunch';

/**
 * The launch exchange (TASK-025c).
 *
 * Nothing in Jest performs an OAuth redirect, so the browser is faked and what
 * is actually under test is the decision each of its answers produces. The
 * outcomes are not interchangeable: a cancellation is a provider's decision and
 * a failure is not, and a launch that half-completed must leave this app holding
 * nothing at all.
 */

const ISS = 'https://ehr.example.com/fhir';
const RETURN_URI = 'medauth://launch';
const SESSION: LaunchSession = { launchId: 'launch-7', ehrType: 'athena', expiresInSeconds: 3600 };

function fakeApi(
  redeem: (claim: string) => Promise<ApiResult<LaunchSession>> = async () => ({
    ok: true,
    value: SESSION,
  }),
): LaunchApi & { redeemed: string[] } {
  const redeemed: string[] = [];
  return {
    redeemed,
    authorizeUrl: ({ iss, launch }) =>
      `https://svc.test/fhir/launch?iss=${encodeURIComponent(iss)}&delivery=mobile${
        launch === undefined ? '' : `&launch=${launch}`
      }`,
    redeemClaim: async (claim) => {
      redeemed.push(claim);
      return redeem(claim);
    },
  };
}

function opener(result: AuthSessionResult): AuthSessionOpener {
  return async () => result;
}

it('redeems the claim off the redirect and yields the launch', async () => {
  const api = fakeApi();

  const outcome = await performSmartLaunch({
    request: { iss: ISS },
    returnUri: RETURN_URI,
    api,
    open: opener({ type: 'success', url: 'medauth://launch?claim=code-1' }),
  });

  expect(outcome).toEqual({ kind: 'launched', session: SESSION });
  expect(api.redeemed).toEqual(['code-1']);
});

it('opens the authorize URL the API built, at the app-s own return target', async () => {
  const api = fakeApi();
  const open = jest.fn<Promise<AuthSessionResult>, [string, string]>(async () => ({
    type: 'success',
    url: 'medauth://launch?claim=code-1',
  }));

  await performSmartLaunch({
    request: { iss: ISS, launch: 'ctx-9' },
    returnUri: RETURN_URI,
    api,
    open,
  });

  const [url, returnUri] = open.mock.calls[0]!;
  expect(url).toContain('delivery=mobile');
  expect(url).toContain('launch=ctx-9');
  // The second argument is what tells the browser session which redirect ends
  // it. A value disagreeing with the service's own copy hangs the session.
  expect(returnUri).toBe(RETURN_URI);
});

/**
 * A provider who closes the login window decided not to start a visit. Reporting
 * that as a failure would be false, and the screen renders the two differently.
 */
it('reports a closed login window as a cancellation, not a failure', async () => {
  const api = fakeApi();

  const outcome = await performSmartLaunch({
    request: { iss: ISS },
    returnUri: RETURN_URI,
    api,
    open: opener({ type: 'cancel' }),
  });

  expect(outcome).toEqual({ kind: 'cancelled' });
  expect(api.redeemed).toHaveLength(0);
});

it('reports a dismissed session as a cancellation too', async () => {
  const outcome = await performSmartLaunch({
    request: { iss: ISS },
    returnUri: RETURN_URI,
    api: fakeApi(),
    open: opener({ type: 'dismiss' }),
  });

  expect(outcome).toEqual({ kind: 'cancelled' });
});

/**
 * `locked` — another auth session already open — is a failure rather than a
 * cancellation, because the provider decided nothing and there is something to
 * report.
 */
it('reports a browser that would not open as a failure', async () => {
  const outcome = await performSmartLaunch({
    request: { iss: ISS },
    returnUri: RETURN_URI,
    api: fakeApi(),
    open: opener({ type: 'locked' }),
  });

  expect(outcome).toEqual({ kind: 'failed', message: BROWSER_UNAVAILABLE_MESSAGE });
});

it('does not surface a thrown browser error, which can name the authorize URL', async () => {
  const outcome = await performSmartLaunch({
    request: { iss: ISS },
    returnUri: RETURN_URI,
    api: fakeApi(),
    open: async () => {
      throw new Error(`failed opening https://svc.test/fhir/launch?iss=${ISS}&launch=ctx-9`);
    },
  });

  expect(outcome).toEqual({ kind: 'failed', message: BROWSER_UNAVAILABLE_MESSAGE });
});

/**
 * A redirect with no claim code is the configuration mismatch this flow fails
 * silently on otherwise — a return URI the app and the service disagree about,
 * or a scheme `app.json` does not register. It is reported, and never retried.
 */
it('reports a redirect carrying no claim code', async () => {
  const api = fakeApi();

  const outcome = await performSmartLaunch({
    request: { iss: ISS },
    returnUri: RETURN_URI,
    api,
    open: opener({ type: 'success', url: 'medauth://launch' }),
  });

  expect(outcome).toEqual({ kind: 'failed', message: NO_CLAIM_MESSAGE });
  expect(api.redeemed).toHaveLength(0);
});

it('reports a spent claim code as a failure, holding no launch', async () => {
  const api = fakeApi(async () => ({
    ok: false,
    failure: {
      kind: 'status',
      status: 404,
      code: 'SMART_UNKNOWN_CLAIM',
      message: 'No such launch claim.',
    },
  }));

  const outcome = await performSmartLaunch({
    request: { iss: ISS },
    returnUri: RETURN_URI,
    api,
    open: opener({ type: 'success', url: 'medauth://launch?claim=code-1' }),
  });

  expect(outcome.kind).toBe('failed');
  if (outcome.kind === 'failed') {
    expect(outcome.message).toContain('No such launch claim.');
  }
});

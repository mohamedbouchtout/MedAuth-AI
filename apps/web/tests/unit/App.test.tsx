import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { App } from '../../src/App';
import type { LaunchApi, LaunchSession } from '../../src/api/fhirClient';
import {
  DECLINED_MESSAGE,
  LAUNCH_FAILED_MESSAGE,
  REDEMPTION_FAILED_MESSAGE,
} from '../../src/launch/smartLaunch';
import { NO_ISSUER_MESSAGE } from '../../src/screens/LaunchScreen';

/**
 * The app root, which is where a completed SMART launch is collected (TASK-070).
 *
 * The properties under test are the ones a browser makes easy to get wrong: a
 * claim code must be spent exactly once, it must leave the URL whatever the
 * outcome, and the moment between finding one and holding a launch must not be
 * rendered as "no launch" — which would invite a provider who has just signed in
 * to sign in again.
 */

const SESSION: LaunchSession = {
  launchId: 'launch-7',
  ehrType: 'athena',
  expiresInSeconds: 3600,
};

function locationAt(search: string): Location {
  return { pathname: '/launch', search } as Location;
}

function launchesThatRedeem(): LaunchApi {
  return {
    authorizeUrl: () => 'https://fhir.test/fhir/launch?delivery=web',
    redeemClaim: vi.fn(async () => ({ ok: true as const, value: SESSION })),
  };
}

function renderApp(search: string, launches: LaunchApi = launchesThatRedeem()) {
  const history = { replaceState: vi.fn() } as unknown as History;
  render(<App launches={launches} location={locationAt(search)} history={history} />);
  return { history, launches };
}

describe('a plain load', () => {
  /**
   * `SMART_ISS` is empty in the test environment, which is a real deployment
   * state: no EHR configured means no standalone launch to offer, and the screen
   * says so rather than showing a button that cannot work.
   */
  it('offers the launch screen and redeems nothing', () => {
    const launches = launchesThatRedeem();
    renderApp('', launches);

    expect(screen.getByTestId('no-issuer')).toHaveTextContent(NO_ISSUER_MESSAGE);
    expect(launches.redeemClaim).not.toHaveBeenCalled();
  });
});

describe('returning from a launch', () => {
  it('redeems the claim and moves on to the visit', async () => {
    const launches = launchesThatRedeem();
    renderApp('?claim=code-1', launches);

    await waitFor(() => expect(launches.redeemClaim).toHaveBeenCalledWith('code-1'));
    await waitFor(() => expect(screen.getByTestId('patient-picker')).toBeInTheDocument());
  });

  /**
   * The gap between finding a code and holding a launch is its own state. A
   * provider who has just signed in must not be shown the sign-in screen while
   * the redemption is in flight.
   */
  it('never shows the sign-in screen while redeeming', () => {
    renderApp('?claim=code-1');

    expect(screen.getByTestId('redeeming')).toBeInTheDocument();
    expect(screen.queryByTestId('launch-screen')).not.toBeInTheDocument();
  });

  /**
   * The code is a credential, and whether it redeems successfully has no bearing
   * on whether it should still be sitting in the address bar and in session
   * history. Scrubbed before the request is even made.
   */
  it('takes the code out of the URL', async () => {
    const { history } = renderApp('?claim=code-1');

    await waitFor(() => expect(history.replaceState).toHaveBeenCalledWith(null, '', '/launch'));
  });

  /**
   * A claim code is single-use: a second attempt is a 404 by design, which would
   * present as a failed launch that had in fact succeeded. React's StrictMode
   * double-invokes effects precisely to surface this.
   */
  it('redeems a code exactly once', async () => {
    const launches = launchesThatRedeem();
    const history = { replaceState: vi.fn() } as unknown as History;
    const { rerender } = render(
      <App launches={launches} location={locationAt('?claim=code-1')} history={history} />,
    );
    rerender(<App launches={launches} location={locationAt('?claim=code-1')} history={history} />);

    await waitFor(() => expect(launches.redeemClaim).toHaveBeenCalledTimes(1));
  });

  it('reports a refused redemption on the launch screen', async () => {
    const launches: LaunchApi = {
      authorizeUrl: () => 'https://fhir.test/fhir/launch?delivery=web',
      redeemClaim: vi.fn(async () => ({
        ok: false as const,
        failure: {
          kind: 'status' as const,
          status: 404,
          code: 'FHIR_UNKNOWN_CLAIM',
          message: 'No such claim.',
        },
      })),
    };
    renderApp('?claim=code-1', launches);

    await waitFor(() =>
      expect(screen.getByTestId('launch-failed')).toHaveTextContent(REDEMPTION_FAILED_MESSAGE),
    );
  });

  /**
   * An empty `?claim=` names no launch, so it is a plain load rather than a
   * redemption that will be refused.
   */
  it('treats an empty claim as no launch at all', () => {
    const launches = launchesThatRedeem();
    renderApp('?claim=', launches);

    expect(launches.redeemClaim).not.toHaveBeenCalled();
    expect(screen.getByTestId('launch-screen')).toBeInTheDocument();
  });
});

/**
 * A launch that came back having failed (TASK-051g).
 *
 * The gap this closes is the one this app could do nothing about from its own
 * side: a browser launch navigates the page away, so a provider who declined at
 * the EHR was previously left on the service's JSON error document and this app
 * never ran. Now the failure is delivered here, and the property that matters is
 * that it is **distinct from a plain load** — the two rendered identically
 * before, which is what let a refused sign-in look like nothing having happened.
 */
describe('returning from a failed launch', () => {
  it('reports a decline on the launch screen', () => {
    const launches = launchesThatRedeem();
    renderApp('?error=declined', launches);

    expect(screen.getByTestId('launch-failed')).toHaveTextContent(DECLINED_MESSAGE);
    expect(launches.redeemClaim).not.toHaveBeenCalled();
  });

  it('reports a generic failure distinctly from a decline', () => {
    renderApp('?error=failed');

    expect(screen.getByTestId('launch-failed')).toHaveTextContent(LAUNCH_FAILED_MESSAGE);
  });

  /**
   * The distinction this delivery exists to make. A plain load shows the launch
   * screen with nothing reported; a failed launch shows the same screen saying
   * what happened. Asserted together so neither can drift into the other.
   */
  it('is distinct from a plain load, which reports nothing', () => {
    renderApp('');

    expect(screen.queryByTestId('launch-failed')).not.toBeInTheDocument();
    expect(screen.getByTestId('launch-screen')).toBeInTheDocument();
  });

  /**
   * An `error` is not a credential, but leaving it in the URL would make a
   * reload replay a failure the provider has already been told about.
   */
  it('takes the error out of the URL', async () => {
    const { history } = renderApp('?error=declined');

    await waitFor(() => expect(history.replaceState).toHaveBeenCalledWith(null, '', '/launch'));
  });

  /**
   * The message is held in state rather than re-derived from the URL, so
   * scrubbing the parameter must not erase what the provider is reading.
   */
  it('keeps the message after the URL has been scrubbed', async () => {
    const { history } = renderApp('?error=declined');

    await waitFor(() => expect(history.replaceState).toHaveBeenCalled());
    expect(screen.getByTestId('launch-failed')).toHaveTextContent(DECLINED_MESSAGE);
  });

  /**
   * A member this build does not know still means the launch did not complete.
   * Rendering nothing would be the silence the delivery exists to end.
   */
  it('still reports a failure it cannot name precisely', () => {
    renderApp('?error=some_future_member');

    expect(screen.getByTestId('launch-failed')).toHaveTextContent(LAUNCH_FAILED_MESSAGE);
  });
});

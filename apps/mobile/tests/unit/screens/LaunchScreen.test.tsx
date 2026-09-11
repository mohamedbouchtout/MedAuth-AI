import { fireEvent, render, waitFor } from '@testing-library/react-native';

import type { ApiResult } from '@medauth/session-client';

import type { LaunchApi, LaunchRequest, LaunchSession } from '@medauth/fhir-client';
import type { AuthSessionOpener, AuthSessionResult } from '../../../src/launch/smartLaunch';
import {
  CANCELLED_MESSAGE,
  LaunchScreen,
  NO_ISS_MESSAGE,
} from '../../../src/screens/LaunchScreen';

/**
 * The EHR sign-in screen (TASK-025c).
 *
 * What this screen decides is when a launch is attempted and what a provider is
 * told about one that did not produce a launch. Both matter more than they look:
 * an EHR launch that waited for a tap would ask a provider to express an
 * intention they already expressed by opening MedAuth from the chart, and a
 * cancellation rendered as a failure tells them something broke when nothing
 * did.
 */

const ISS = 'https://ehr.example.com/fhir';
const SESSION: LaunchSession = { launchId: 'launch-7', ehrType: 'athena', expiresInSeconds: 3600 };

function fakeApi(
  redeem: () => Promise<ApiResult<LaunchSession>> = async () => ({ ok: true, value: SESSION }),
): LaunchApi & { requests: LaunchRequest[] } {
  const requests: LaunchRequest[] = [];
  return {
    requests,
    authorizeUrl: (request) => {
      requests.push(request);
      return 'https://svc.test/fhir/launch?delivery=mobile';
    },
    redeemClaim: redeem,
  };
}

function opener(result: AuthSessionResult): AuthSessionOpener {
  return async () => result;
}

const SUCCESS = opener({ type: 'success', url: 'medauth://launch?claim=code-1' });

it('waits for a tap when the launch is standalone', async () => {
  const onLaunched = jest.fn();
  const api = fakeApi();

  const view = await render(
    <LaunchScreen onLaunched={onLaunched} iss={ISS} api={api} open={SUCCESS} />,
  );

  // Nobody has asked for anything yet: no browser is opened until they do.
  expect(api.requests).toHaveLength(0);
  fireEvent.press(view.getByTestId('start-launch'));

  await waitFor(() => expect(onLaunched).toHaveBeenCalledWith(SESSION));
  expect(api.requests).toEqual([{ iss: ISS }]);
});

/**
 * The EHR launch starts itself. The provider expressed their intent by opening
 * MedAuth from the chart, and the opaque `launch` context is carried through
 * exactly as it arrived — it is the only reason the completed launch knows which
 * patient is on the chart.
 */
it('starts an EHR launch on its own, carrying the launch context', async () => {
  const onLaunched = jest.fn();
  const api = fakeApi();

  await render(
    <LaunchScreen
      onLaunched={onLaunched}
      inbound={{ iss: ISS, launch: 'ctx-9' }}
      api={api}
      open={SUCCESS}
    />,
  );

  await waitFor(() => expect(onLaunched).toHaveBeenCalledWith(SESSION));
  expect(api.requests).toEqual([{ iss: ISS, launch: 'ctx-9' }]);
});

it('attempts an inbound launch once, not once per render', async () => {
  const onLaunched = jest.fn();
  const api = fakeApi();
  const inbound: LaunchRequest = { iss: ISS, launch: 'ctx-9' };

  const view = await render(
    <LaunchScreen onLaunched={onLaunched} inbound={inbound} api={api} open={SUCCESS} />,
  );
  await waitFor(() => expect(api.requests).toHaveLength(1));

  // A re-render with the same launch is not a second launch. Without the guard
  // this opens a browser window per render.
  view.rerender(
    <LaunchScreen onLaunched={onLaunched} inbound={inbound} api={api} open={SUCCESS} />,
  );

  await waitFor(() => expect(onLaunched).toHaveBeenCalled());
  expect(api.requests).toHaveLength(1);
});

it('states a cancellation plainly and offers the button again', async () => {
  const onLaunched = jest.fn();

  const view = await render(
    <LaunchScreen
      onLaunched={onLaunched}
      iss={ISS}
      api={fakeApi()}
      open={opener({ type: 'cancel' })}
    />,
  );
  fireEvent.press(view.getByTestId('start-launch'));

  await waitFor(() => expect(view.getByTestId('launch-cancelled')).toBeTruthy());
  expect(view.getByText(CANCELLED_MESSAGE)).toBeTruthy();
  // Not an error: a provider who closed the window decided not to start a visit.
  expect(view.queryByTestId('launch-error')).toBeNull();
  expect(view.getByTestId('start-launch')).toBeTruthy();
  expect(onLaunched).not.toHaveBeenCalled();
});

it('reports a launch that failed, and hands on nothing', async () => {
  const onLaunched = jest.fn();
  const api = fakeApi(async () => ({
    ok: false,
    failure: {
      kind: 'status',
      status: 404,
      code: 'SMART_UNKNOWN_CLAIM',
      message: 'No such launch claim.',
    },
  }));

  const view = await render(
    <LaunchScreen onLaunched={onLaunched} iss={ISS} api={api} open={SUCCESS} />,
  );
  fireEvent.press(view.getByTestId('start-launch'));

  await waitFor(() => expect(view.getByTestId('launch-error')).toBeTruthy());
  // A failed launch leaves this app holding nothing at all. A partial one would
  // surface later as a 404 from a patient route rather than as a launch that did
  // not happen.
  expect(onLaunched).not.toHaveBeenCalled();
});

/**
 * A build with no issuer cannot sign in to anything, and says so instead of
 * showing a button that cannot work. The message is administrator-facing on
 * purpose: a provider cannot act on it, and "try again" would send them round a
 * loop that cannot terminate.
 */
it('refuses to offer a launch when no EHR is configured', async () => {
  const api = fakeApi();

  const view = await render(
    <LaunchScreen onLaunched={jest.fn()} iss="" api={api} open={SUCCESS} />,
  );

  expect(view.getByTestId('launch-unconfigured')).toBeTruthy();
  expect(view.getByText(NO_ISS_MESSAGE)).toBeTruthy();
  expect(view.queryByTestId('start-launch')).toBeNull();
  expect(api.requests).toHaveLength(0);
});

/**
 * An EHR launch names its own issuer, so an unconfigured build still serves one.
 * The configured value is the standalone path's input and nothing else.
 */
it('still runs an EHR launch when no issuer is configured', async () => {
  const onLaunched = jest.fn();
  const api = fakeApi();

  await render(
    <LaunchScreen
      onLaunched={onLaunched}
      iss=""
      inbound={{ iss: ISS, launch: 'ctx-9' }}
      api={api}
      open={SUCCESS}
    />,
  );

  await waitFor(() => expect(onLaunched).toHaveBeenCalledWith(SESSION));
});

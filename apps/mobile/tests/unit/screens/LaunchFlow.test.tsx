import { fireEvent, render, waitFor } from '@testing-library/react-native';

import type { ApiResult, Session, SessionsApi, StartVisitInput } from '../../../src/api/sessions';
import type { FhirApi, LaunchContext, PatientSearchResults } from '../../../src/api/fhir';
import type { LaunchApi, LaunchSession } from '../../../src/api/launch';
import type { AuthSessionOpener, AuthSessionResult } from '../../../src/launch/smartLaunch';
import { LaunchFlow } from '../../../src/screens/LaunchFlow';

/**
 * TASK-025c's three acceptance criteria, end to end.
 *
 * The pieces are tested separately elsewhere. What only this file can show is
 * that the launch survives the handoff into everything TASK-025b built — that an
 * EHR launch reaches the launch-context path and a standalone one reaches the
 * search, which is the distinction the whole task turns on, and that a
 * cancelled launch leaves the app refusing rather than half-configured.
 *
 * Nothing here performs an OAuth redirect: the browser is faked, and whether an
 * OS actually routes the custom scheme back is verified by hand per TASK-025c.
 * What is under test is everything downstream of that redirect.
 */

jest.mock('../../../src/hooks/useAudioCapture', () => ({
  // The session screen is exercised against the real hook in its own suite; here
  // it only has to get as far as calling startVisit.
  useAudioCapture: () => ({
    state: { status: 'idle' },
    start: jest.fn(async () => undefined),
    stop: jest.fn(),
  }),
}));

const ISS = 'https://ehr.example.com/fhir';
const LAUNCH_ID = 'launch-7';
const PROVIDER_ID = '22222222-2222-4222-8222-222222222222';
const SESSION_ID = '11111111-1111-4111-8111-111111111111';
const LAUNCH: LaunchSession = { launchId: LAUNCH_ID, ehrType: 'athena', expiresInSeconds: 3600 };

const SANCHEZ = {
  patientId: 'synthea-123',
  familyName: 'Sanchez',
  givenNames: ['Aurelio'],
  birthDate: '1962-04-17',
  gender: 'male',
};

function fakeLaunchApi(): LaunchApi {
  return {
    authorizeUrl: () => 'https://svc.test/fhir/launch?delivery=mobile',
    redeemClaim: async (): Promise<ApiResult<LaunchSession>> => ({ ok: true, value: LAUNCH }),
  };
}

function fakeFhir(context: LaunchContext): FhirApi & { launchIds: string[] } {
  const launchIds: string[] = [];
  return {
    launchIds,
    getLaunchContext: async (launchId): Promise<ApiResult<LaunchContext>> => {
      launchIds.push(launchId);
      return { ok: true, value: context };
    },
    searchPatients: async (launchId): Promise<ApiResult<PatientSearchResults>> => {
      launchIds.push(launchId);
      return { ok: true, value: { matches: [SANCHEZ], truncated: false } };
    },
  };
}

function fakeSessions(): SessionsApi & { started: StartVisitInput[] } {
  const started: StartVisitInput[] = [];
  return {
    started,
    startVisit: async (input): Promise<ApiResult<Session>> => {
      started.push(input);
      return { ok: true, value: { sessionId: SESSION_ID, jwt: 'a.b.c' } };
    },
    remintToken: async (): Promise<ApiResult<Session>> => ({
      ok: true,
      value: { sessionId: SESSION_ID, jwt: 'a.b.c' },
    }),
    endVisit: async (): Promise<ApiResult<void>> => ({ ok: true, value: undefined }),
  };
}

function opener(result: AuthSessionResult): AuthSessionOpener {
  return async () => result;
}

const SUCCESS = opener({ type: 'success', url: 'medauth://launch?claim=code-1' });

it('carries an EHR launch through to the chart-s own patient', async () => {
  const sessions = fakeSessions();
  const fhir = fakeFhir({ patientId: 'p1', encounterId: 'Encounter/9', providerId: PROVIDER_ID });

  const view = await render(
    <LaunchFlow
      inbound={{ iss: ISS, launch: 'ctx-9' }}
      launch={fakeLaunchApi()}
      open={SUCCESS}
      fhir={fhir}
      sessions={sessions}
    />,
  );

  // The launch starts itself, and the patient screen never offers a search:
  // the EHR already said who is in the room.
  await waitFor(() => expect(view.getByTestId('start-visit')).toBeTruthy());
  expect(view.queryByTestId('patient-query')).toBeNull();

  fireEvent.press(view.getByTestId('start-visit'));

  await waitFor(() => expect(sessions.started).toHaveLength(1));
  // The launch_id obtained here is what reaches both the patient routes and the
  // start-visit body; with the chart entry it is what fills the encounter's
  // payer columns.
  expect(fhir.launchIds).toEqual([LAUNCH_ID]);
  expect(sessions.started[0]).toEqual({
    patientId: 'p1',
    providerId: PROVIDER_ID,
    ehrEncounterId: 'Encounter/9',
    launchId: LAUNCH_ID,
  });
});

it('falls through to search after a standalone launch', async () => {
  const sessions = fakeSessions();
  const fhir = fakeFhir({ patientId: null, encounterId: null, providerId: PROVIDER_ID });

  const view = await render(
    <LaunchFlow
      iss={ISS}
      launch={fakeLaunchApi()}
      open={SUCCESS}
      fhir={fhir}
      sessions={sessions}
    />,
  );

  // No EHR launch arrived, so the provider taps to sign in.
  fireEvent.press(view.getByTestId('start-launch'));

  // A standalone launch named nobody, which is the only thing that makes the
  // search path reachable at all.
  await waitFor(() => expect(view.getByTestId('patient-query')).toBeTruthy());
  fireEvent.changeText(view.getByTestId('patient-query'), 'Sanchez');
  await waitFor(() => expect(view.getByTestId('patient-query').props.value).toBe('Sanchez'));
  fireEvent.press(view.getByTestId('search-patients'));

  await waitFor(() => expect(view.getByTestId(`patient-${SANCHEZ.patientId}`)).toBeTruthy());
  fireEvent.press(view.getByTestId(`patient-${SANCHEZ.patientId}`));

  await waitFor(() => expect(view.getByTestId('start-visit')).toBeTruthy());
  fireEvent.press(view.getByTestId('start-visit'));

  await waitFor(() => expect(sessions.started).toHaveLength(1));
  expect(sessions.started[0]).toEqual({
    patientId: 'synthea-123',
    providerId: PROVIDER_ID,
    launchId: LAUNCH_ID,
  });
});

/**
 * The third criterion, and the one that says what "no partially-configured
 * state" means concretely: a cancelled launch does not advance the flow at all.
 * The patient screen is never reached, so no route is called with a handle that
 * names no EHR credential, and no visit can be started.
 */
it('leaves the app refusing when the provider cancels the sign-in', async () => {
  const sessions = fakeSessions();
  const fhir = fakeFhir({ patientId: 'p1', encounterId: 'e1', providerId: PROVIDER_ID });

  const view = await render(
    <LaunchFlow
      iss={ISS}
      launch={fakeLaunchApi()}
      open={opener({ type: 'cancel' })}
      fhir={fhir}
      sessions={sessions}
    />,
  );
  fireEvent.press(view.getByTestId('start-launch'));

  await waitFor(() => expect(view.getByTestId('launch-cancelled')).toBeTruthy());
  expect(view.queryByTestId('patient-query')).toBeNull();
  expect(view.queryByTestId('start-visit')).toBeNull();
  expect(fhir.launchIds).toHaveLength(0);
  expect(sessions.started).toHaveLength(0);
});

import { fireEvent, render, waitFor } from '@testing-library/react-native';

import type { ApiResult, Session, SessionsApi, StartVisitInput } from '../../../src/api/sessions';
import type { FhirApi, LaunchContext, PatientSearchResults } from '../../../src/api/fhir';
import { VisitFlow } from '../../../src/screens/VisitFlow';

/**
 * TASK-025b's acceptance criterion, end to end: a search returns matches and the
 * selection populates the start-visit call.
 *
 * The two screens are tested separately elsewhere. What only this file can show
 * is that the identifiers survive the handoff between them — that the patient a
 * provider picked, the provider the launch was authorized by, and the launch
 * itself all reach `POST /sessions/start` as the fields that service expects.
 * Losing one there is silent: the visit starts, and the encounter is filed
 * against the wrong patient or with its payer columns left NULL.
 */

jest.mock('../../../src/hooks/useAudioCapture', () => ({
  // The session screen is exercised against the real hook in its own suite;
  // here it only has to get as far as calling startVisit.
  useAudioCapture: () => ({
    state: { status: 'idle' },
    start: jest.fn(async () => undefined),
    stop: jest.fn(),
  }),
}));

const LAUNCH_ID = 'launch-7';
const PROVIDER_ID = '22222222-2222-4222-8222-222222222222';
const SESSION_ID = '11111111-1111-4111-8111-111111111111';

const SANCHEZ = {
  patientId: 'synthea-123',
  familyName: 'Sanchez',
  givenNames: ['Aurelio'],
  birthDate: '1962-04-17',
  gender: 'male',
};

function fakeFhir(
  context: LaunchContext,
  searchResult: PatientSearchResults = { matches: [SANCHEZ], truncated: false },
): FhirApi {
  return {
    getLaunchContext: async (): Promise<ApiResult<LaunchContext>> => ({
      ok: true,
      value: context,
    }),
    searchPatients: async (): Promise<ApiResult<PatientSearchResults>> => ({
      ok: true,
      value: searchResult,
    }),
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

it('carries a searched-for patient into the start-visit call', async () => {
  const sessions = fakeSessions();
  const fhir = fakeFhir({ patientId: null, encounterId: null, providerId: PROVIDER_ID });

  const view = await render(<VisitFlow launchId={LAUNCH_ID} fhir={fhir} sessions={sessions} />);

  // A standalone launch named nobody, so the search is offered.
  await waitFor(() => expect(view.getByTestId('patient-query')).toBeTruthy());
  fireEvent.changeText(view.getByTestId('patient-query'), 'Sanchez');
  await waitFor(() => expect(view.getByTestId('patient-query').props.value).toBe('Sanchez'));
  fireEvent.press(view.getByTestId('search-patients'));

  await waitFor(() => expect(view.getByTestId(`patient-${SANCHEZ.patientId}`)).toBeTruthy());
  fireEvent.press(view.getByTestId(`patient-${SANCHEZ.patientId}`));

  // The picker is done; the session screen takes over.
  await waitFor(() => expect(view.getByTestId('start-visit')).toBeTruthy());
  fireEvent.press(view.getByTestId('start-visit'));

  await waitFor(() => expect(sessions.started).toHaveLength(1));
  expect(sessions.started[0]).toEqual({
    patientId: 'synthea-123',
    providerId: PROVIDER_ID,
    launchId: LAUNCH_ID,
  });
});

it('carries an EHR launch straight through, with the chart entry', async () => {
  const sessions = fakeSessions();
  const fhir = fakeFhir({ patientId: 'p1', encounterId: 'Encounter/9', providerId: PROVIDER_ID });

  const view = await render(<VisitFlow launchId={LAUNCH_ID} fhir={fhir} sessions={sessions} />);

  // No search at any point: the EHR already said who is in the room.
  await waitFor(() => expect(view.getByTestId('start-visit')).toBeTruthy());
  expect(view.queryByTestId('patient-query')).toBeNull();

  fireEvent.press(view.getByTestId('start-visit'));

  await waitFor(() => expect(sessions.started).toHaveLength(1));
  // ehrEncounterId and launchId together are what let the service fill the
  // encounter's payer columns; either alone leaves them NULL.
  expect(sessions.started[0]).toEqual({
    patientId: 'p1',
    providerId: PROVIDER_ID,
    ehrEncounterId: 'Encounter/9',
    launchId: LAUNCH_ID,
  });
});

it('never reaches the session screen when no launch is held', async () => {
  const sessions = fakeSessions();
  const fhir = fakeFhir({ patientId: 'p1', encounterId: 'e1', providerId: PROVIDER_ID });

  const view = await render(<VisitFlow launchId={null} fhir={fhir} sessions={sessions} />);

  // This is every build until TASK-025c, and it is deliberate: a visit started
  // against an invented identifier is silent at every layer below this.
  expect(view.getByTestId('picker-error')).toBeTruthy();
  expect(view.queryByTestId('start-visit')).toBeNull();
  expect(sessions.started).toHaveLength(0);
});

import { fireEvent, render, waitFor } from '@testing-library/react-native';

import type { ApiResult } from '@medauth/session-client';

import type { FhirApi, LaunchContext, PatientSearchResults } from '../../../src/api/fhir';
import {
  NO_LAUNCH_MESSAGE,
  NO_MATCHES_MESSAGE,
  NO_PROVIDER_MESSAGE,
  PatientPickerScreen,
  TRUNCATED_MESSAGE,
} from '../../../src/screens/PatientPickerScreen';
import type { VisitSubject } from '../../../src/session/patientSource';

/**
 * The screen that decides which patient a visit is about (TASK-025b).
 *
 * The load-bearing behaviour is that an EHR launch never sees a search box. A
 * search offered there would let a provider start a visit against a different
 * patient than the chart in front of them, and nothing downstream could tell.
 *
 * Queries go through the render result rather than the global `screen`, matching
 * `SessionScreen.test.tsx` — the convention this app's suite already follows.
 */

const LAUNCH_ID = 'launch-7';
const PROVIDER_ID = '22222222-2222-4222-8222-222222222222';

const SANCHEZ = {
  patientId: 'synthea-123',
  familyName: 'Sanchez',
  givenNames: ['Aurelio', 'Luis'],
  birthDate: '1962-04-17',
  gender: 'male',
};

type Picker = Awaited<ReturnType<typeof render>>;

function context(overrides: Partial<LaunchContext> = {}): LaunchContext {
  return { patientId: 'p1', encounterId: 'e1', providerId: PROVIDER_ID, ...overrides };
}

function fakeFhir(
  launch: ApiResult<LaunchContext>,
  searchResult: ApiResult<PatientSearchResults> = {
    ok: true,
    value: { matches: [], truncated: false },
  },
): FhirApi & { searches: [string, string, string | undefined][] } {
  const searches: [string, string, string | undefined][] = [];
  return {
    searches,
    getLaunchContext: async () => launch,
    searchPatients: async (launchId, query, birthDate) => {
      searches.push([launchId, query, birthDate]);
      return searchResult;
    },
  };
}

/**
 * Type into a field and wait for the component to hold the value.
 *
 * `fireEvent.changeText` schedules a state update React has not necessarily
 * flushed by the time the next `fireEvent.press` runs, and the press handler
 * reads component state — so without this wait the search fires with an empty
 * query and the test proves nothing. Waiting on the controlled `value` is the
 * assertion that the render actually happened.
 */
async function typeInto(view: Picker, testID: string, text: string): Promise<void> {
  fireEvent.changeText(view.getByTestId(testID), text);
  await waitFor(() => expect(view.getByTestId(testID).props.value).toBe(text));
}

/**
 * `render` is awaited, as it is in `SessionScreen.test.tsx`.
 *
 * It resolves after the initial render in this setup, and a non-awaited result
 * has no query methods on it at all — which surfaces as "render has not been
 * called" from a query rather than as anything naming the real cause.
 */
async function renderPicker(
  fhir: FhirApi,
  launchId: string | null = LAUNCH_ID,
): Promise<{ view: Picker; resolved: VisitSubject[] }> {
  const resolved: VisitSubject[] = [];
  const view = await render(
    <PatientPickerScreen
      launchId={launchId}
      onResolved={(subject) => resolved.push(subject)}
      fhir={fhir}
    />,
  );
  return { view, resolved };
}

/** Fill the search box and press Search, once the box exists. */
async function search(view: Picker, query: string, birthDate?: string): Promise<void> {
  await waitFor(() => expect(view.getByTestId('patient-query')).toBeTruthy());
  await typeInto(view, 'patient-query', query);
  if (birthDate !== undefined) {
    await typeInto(view, 'patient-birth-date', birthDate);
  }
  fireEvent.press(view.getByTestId('search-patients'));
}

describe('an EHR launch', () => {
  it('resolves the patient the EHR named and never offers a search', async () => {
    const { view, resolved } = await renderPicker(fakeFhir({ ok: true, value: context() }));

    await waitFor(() => expect(resolved).toHaveLength(1));
    expect(resolved[0]).toEqual({
      patientId: 'p1',
      providerId: PROVIDER_ID,
      ehrEncounterId: 'e1',
      launchId: LAUNCH_ID,
    });
    // The whole point: no search box was ever rendered.
    expect(view.queryByTestId('patient-query')).toBeNull();
  });
});

describe('a launch this app cannot act on', () => {
  it('refuses when there is no launch at all, and names what is missing', async () => {
    const { view, resolved } = await renderPicker(fakeFhir({ ok: true, value: context() }), null);

    expect(view.getByTestId('picker-error')).toBeTruthy();
    expect(view.getByText(NO_LAUNCH_MESSAGE)).toBeTruthy();
    expect(resolved).toHaveLength(0);
  });

  it('refuses a launch whose provider could not be verified', async () => {
    const { view, resolved } = await renderPicker(
      fakeFhir({ ok: true, value: context({ providerId: null }) }),
    );

    await waitFor(() => expect(view.getByText(NO_PROVIDER_MESSAGE)).toBeTruthy());
    // Not a search: finding a patient would not produce a provider, so the
    // refusal belongs here rather than after someone has been picked.
    expect(view.queryByTestId('patient-query')).toBeNull();
    expect(resolved).toHaveLength(0);
  });

  it('reports a launch that could not be read', async () => {
    const { view, resolved } = await renderPicker(
      fakeFhir({
        ok: false,
        failure: {
          kind: 'status',
          status: 404,
          code: 'FHIR_UNKNOWN_LAUNCH',
          message: 'No such SMART launch.',
        },
      }),
    );

    await waitFor(() => expect(view.getByTestId('picker-error')).toBeTruthy());
    expect(view.getByText(/No such SMART launch\./)).toBeTruthy();
    expect(resolved).toHaveLength(0);
  });
});

describe('a standalone launch', () => {
  const standalone: ApiResult<LaunchContext> = {
    ok: true,
    value: context({ patientId: null, encounterId: null }),
  };

  it('offers a search, and a selection resolves the subject', async () => {
    const fhir = fakeFhir(standalone, {
      ok: true,
      value: { matches: [SANCHEZ], truncated: false },
    });
    const { view, resolved } = await renderPicker(fhir);

    await search(view, 'Sanchez');

    await waitFor(() => expect(view.getByText('Aurelio Luis Sanchez')).toBeTruthy());
    // The date of birth is shown because this is the last point at which a wrong
    // pick can be noticed by a human.
    expect(view.getByText(/1962-04-17/)).toBeTruthy();

    fireEvent.press(view.getByTestId(`patient-${SANCHEZ.patientId}`));

    await waitFor(() => expect(resolved).toHaveLength(1));
    expect(resolved[0]).toEqual({
      patientId: 'synthea-123',
      providerId: PROVIDER_ID,
      launchId: LAUNCH_ID,
    });
    // No chart entry: a standalone launch corresponds to none, and inventing one
    // would file the note against a visit the EHR does not have.
    expect(resolved[0]!.ehrEncounterId).toBeUndefined();
  });

  it('passes the date of birth through when one is given', async () => {
    const fhir = fakeFhir(standalone, {
      ok: true,
      value: { matches: [SANCHEZ], truncated: false },
    });
    const { view } = await renderPicker(fhir);

    await search(view, 'Sanchez', '1962-04-17');

    await waitFor(() => expect(fhir.searches).toHaveLength(1));
    expect(fhir.searches[0]).toEqual([LAUNCH_ID, 'Sanchez', '1962-04-17']);
  });

  it('does not search on an empty query', async () => {
    const fhir = fakeFhir(standalone);
    const { view } = await renderPicker(fhir);

    await search(view, '   ');

    expect(fhir.searches).toHaveLength(0);
  });

  it('says so when nobody matched, rather than showing an empty screen', async () => {
    const fhir = fakeFhir(standalone, { ok: true, value: { matches: [], truncated: false } });
    const { view } = await renderPicker(fhir);

    await search(view, 'Nobody');

    await waitFor(() => expect(view.getByText(NO_MATCHES_MESSAGE)).toBeTruthy());
  });

  it('tells the provider when there were more matches than are shown', async () => {
    // A provider shown a short list and not told it is short concludes the
    // patient is not in the system.
    const fhir = fakeFhir(standalone, {
      ok: true,
      value: { matches: [SANCHEZ], truncated: true },
    });
    const { view } = await renderPicker(fhir);

    await search(view, 'Smith');

    await waitFor(() => expect(view.getByText(TRUNCATED_MESSAGE)).toBeTruthy());
  });

  it('reports a search that failed and leaves the box usable', async () => {
    const fhir = fakeFhir(standalone, {
      ok: false,
      failure: { kind: 'network', message: 'MedAuth AI could not reach the server.' },
    });
    const { view } = await renderPicker(fhir);

    await search(view, 'Sanchez');

    await waitFor(() => expect(view.getByTestId('search-error')).toBeTruthy());
    expect(view.getByTestId('patient-query')).toBeTruthy();
  });

  it('labels a patient the EHR holds no name for rather than rendering a blank row', async () => {
    const nameless = {
      patientId: 'p9',
      familyName: null,
      givenNames: [],
      birthDate: null,
      gender: null,
    };
    const fhir = fakeFhir(standalone, {
      ok: true,
      value: { matches: [nameless], truncated: false },
    });
    const { view, resolved } = await renderPicker(fhir);

    await search(view, 'p9');

    await waitFor(() => expect(view.getByText('Unnamed patient (p9)')).toBeTruthy());
    expect(view.getByText(/no date of birth recorded/)).toBeTruthy();

    fireEvent.press(view.getByTestId('patient-p9'));
    await waitFor(() => expect(resolved).toHaveLength(1));
  });
});

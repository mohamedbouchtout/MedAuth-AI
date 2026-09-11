import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { FhirApi, LaunchContext, PatientSearchResults } from '@medauth/fhir-client';

import {
  NO_LAUNCH_MESSAGE,
  NO_MATCHES_MESSAGE,
  NO_PROVIDER_MESSAGE,
  PatientPicker,
  TRUNCATED_MESSAGE,
} from '../../../src/screens/PatientPicker';

/**
 * Which patient a visit is about (TASK-070).
 *
 * The order is the substance: an EHR launch has already named the patient, so no
 * search is offered at all. Offering one would let a provider start a visit
 * against a different patient than the chart in front of them, and nothing below
 * that point would catch it. The order itself lives in `@medauth/fhir-client` —
 * these tests assert this screen actually honours it.
 */

const LAUNCH_ID = 'launch-7';
const PROVIDER_ID = '22222222-2222-4222-8222-222222222222';

function fhirWith(
  context: LaunchContext,
  search: PatientSearchResults = { matches: [], truncated: false },
): FhirApi {
  return {
    getLaunchContext: vi.fn(async () => ({ ok: true as const, value: context })),
    searchPatients: vi.fn(async () => ({ ok: true as const, value: search })),
  };
}

function renderPicker(fhir: FhirApi, launchId: string | null = LAUNCH_ID) {
  const onResolved = vi.fn();
  render(<PatientPicker launchId={launchId} onResolved={onResolved} fhir={fhir} />);
  return { onResolved };
}

describe('after an EHR launch', () => {
  it('resolves the patient the EHR named and never offers a search', async () => {
    const fhir = fhirWith({
      patientId: 'patient-1',
      encounterId: 'Encounter/7',
      providerId: PROVIDER_ID,
    });
    const { onResolved } = renderPicker(fhir);

    await waitFor(() =>
      expect(onResolved).toHaveBeenCalledWith({
        patientId: 'patient-1',
        providerId: PROVIDER_ID,
        ehrEncounterId: 'Encounter/7',
        launchId: LAUNCH_ID,
      }),
    );
    expect(screen.queryByTestId('patient-search')).not.toBeInTheDocument();
    expect(fhir.searchPatients).not.toHaveBeenCalled();
  });
});

describe('after a standalone launch', () => {
  it('falls through to the search', async () => {
    const fhir = fhirWith({ patientId: null, encounterId: null, providerId: PROVIDER_ID });
    renderPicker(fhir);

    await waitFor(() => expect(screen.getByTestId('patient-search')).toBeInTheDocument());
  });

  it('carries the provider from the launch onto the picked patient', async () => {
    const fhir = fhirWith(
      { patientId: null, encounterId: null, providerId: PROVIDER_ID },
      {
        matches: [
          {
            patientId: 'patient-9',
            familyName: 'Smith',
            givenNames: ['Ada'],
            birthDate: '1970-01-01',
            gender: 'female',
          },
        ],
        truncated: false,
      },
    );
    const { onResolved } = renderPicker(fhir);
    await waitFor(() => expect(screen.getByTestId('patient-search')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('patient-query'), { target: { value: 'Smith' } });
    fireEvent.click(screen.getByTestId('patient-search'));
    await waitFor(() => expect(screen.getByTestId('search-results')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Ada Smith'));

    // A search finds a patient and says nothing about who is treating them, so
    // the provider must come from the launch in both paths.
    expect(onResolved).toHaveBeenCalledWith({
      patientId: 'patient-9',
      providerId: PROVIDER_ID,
      launchId: LAUNCH_ID,
    });
  });

  /**
   * A standalone launch corresponds to no chart entry, and inventing one would
   * file the note against a visit the EHR does not have.
   */
  it('carries no chart entry for a patient found by search', async () => {
    const fhir = fhirWith(
      { patientId: null, encounterId: null, providerId: PROVIDER_ID },
      {
        matches: [
          {
            patientId: 'patient-9',
            familyName: 'Smith',
            givenNames: ['Ada'],
            birthDate: null,
            gender: null,
          },
        ],
        truncated: false,
      },
    );
    const { onResolved } = renderPicker(fhir);
    await waitFor(() => expect(screen.getByTestId('patient-search')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('patient-query'), { target: { value: 'Smith' } });
    fireEvent.click(screen.getByTestId('patient-search'));
    await waitFor(() => expect(screen.getByTestId('search-results')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Ada Smith'));

    expect(onResolved.mock.calls[0]?.[0]).not.toHaveProperty('ehrEncounterId');
  });

  it('reports an empty result rather than an empty list', async () => {
    const fhir = fhirWith({ patientId: null, encounterId: null, providerId: PROVIDER_ID });
    renderPicker(fhir);
    await waitFor(() => expect(screen.getByTestId('patient-search')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('patient-query'), { target: { value: 'Nobody' } });
    fireEvent.click(screen.getByTestId('patient-search'));

    await waitFor(() => expect(screen.getByTestId('search-empty')).toHaveTextContent(NO_MATCHES_MESSAGE));
  });

  /**
   * A provider shown twenty of two hundred Smiths and not told so concludes the
   * patient they want is not in the system.
   */
  it('says when the EHR held more matches than were returned', async () => {
    const fhir = fhirWith(
      { patientId: null, encounterId: null, providerId: PROVIDER_ID },
      {
        matches: [
          {
            patientId: 'p',
            familyName: 'Smith',
            givenNames: ['Ada'],
            birthDate: null,
            gender: null,
          },
        ],
        truncated: true,
      },
    );
    renderPicker(fhir);
    await waitFor(() => expect(screen.getByTestId('patient-search')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('patient-query'), { target: { value: 'Smith' } });
    fireEvent.click(screen.getByTestId('patient-search'));

    await waitFor(() =>
      expect(screen.getByTestId('search-truncated')).toHaveTextContent(TRUNCATED_MESSAGE),
    );
  });

  /**
   * The date of birth is what distinguishes two people with one name, so its
   * absence is stated rather than left blank — a provider needs to know they
   * cannot tell these candidates apart here.
   */
  it('says so when a candidate has no recorded date of birth', async () => {
    const fhir = fhirWith(
      { patientId: null, encounterId: null, providerId: PROVIDER_ID },
      {
        matches: [
          {
            patientId: 'p',
            familyName: 'Smith',
            givenNames: ['Ada'],
            birthDate: null,
            gender: null,
          },
        ],
        truncated: false,
      },
    );
    renderPicker(fhir);
    await waitFor(() => expect(screen.getByTestId('patient-search')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('patient-query'), { target: { value: 'Smith' } });
    fireEvent.click(screen.getByTestId('patient-search'));

    await waitFor(() =>
      expect(screen.getByText(/date of birth not recorded/)).toBeInTheDocument(),
    );
  });
});

describe('when a visit cannot be started at all', () => {
  it('refuses when this app holds no launch', () => {
    const fhir = fhirWith({ patientId: null, encounterId: null, providerId: null });
    renderPicker(fhir, null);

    expect(screen.getByTestId('picker-failed')).toHaveTextContent(NO_LAUNCH_MESSAGE);
    expect(fhir.getLaunchContext).not.toHaveBeenCalled();
  });

  /**
   * Searching would not produce a provider either, so this is answered here
   * rather than after a provider has typed a name and picked someone.
   */
  it('refuses when the launch identified no provider', async () => {
    renderPicker(fhirWith({ patientId: 'patient-1', encounterId: null, providerId: null }));

    await waitFor(() =>
      expect(screen.getByTestId('picker-failed')).toHaveTextContent(NO_PROVIDER_MESSAGE),
    );
  });

  it('reports a launch that could not be read', async () => {
    const fhir: FhirApi = {
      getLaunchContext: vi.fn(async () => ({
        ok: false as const,
        failure: {
          kind: 'status' as const,
          status: 404,
          code: 'FHIR_UNKNOWN_LAUNCH',
          message: 'No such launch.',
        },
      })),
      searchPatients: vi.fn(),
    };
    renderPicker(fhir);

    await waitFor(() => expect(screen.getByTestId('picker-failed')).toHaveTextContent('No such launch.'));
  });

  it('reports a failed search without discarding the form', async () => {
    const fhir: FhirApi = {
      getLaunchContext: vi.fn(async () => ({
        ok: true as const,
        value: { patientId: null, encounterId: null, providerId: PROVIDER_ID },
      })),
      searchPatients: vi.fn(async () => ({
        ok: false as const,
        failure: { kind: 'network' as const, message: 'unreachable' },
      })),
    };
    renderPicker(fhir);
    await waitFor(() => expect(screen.getByTestId('patient-search')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('patient-query'), { target: { value: 'Smith' } });
    fireEvent.click(screen.getByTestId('patient-search'));

    await waitFor(() => expect(screen.getByTestId('search-failed')).toHaveTextContent('unreachable'));
    expect(screen.getByTestId('patient-query')).toHaveValue('Smith');
  });
});

describe('the launch is read once', () => {
  /**
   * The regression `apps/mobile`'s picker actually shipped: with `onResolved` in
   * the effect's dependency array, an inline callback re-read the launch on every
   * render — one request per keystroke, discarding the matches already on screen.
   */
  it('does not re-read the launch on every keystroke', async () => {
    const fhir = fhirWith({ patientId: null, encounterId: null, providerId: PROVIDER_ID });
    render(<PatientPicker launchId={LAUNCH_ID} onResolved={() => {}} fhir={fhir} />);
    await waitFor(() => expect(screen.getByTestId('patient-search')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('patient-query'), { target: { value: 'S' } });
    fireEvent.change(screen.getByTestId('patient-query'), { target: { value: 'Sm' } });
    fireEvent.change(screen.getByTestId('patient-query'), { target: { value: 'Smi' } });

    expect(fhir.getLaunchContext).toHaveBeenCalledTimes(1);
  });
});

import type { ApiResult } from '@medauth/session-client';

import type { FhirApi, LaunchContext, PatientSearchResults } from '../../../src/api/fhir';
import {
  patientSelectionUnavailable,
  resolveLaunch,
  subjectFromLaunch,
  subjectFromMatch,
} from '../../../src/session/patientSource';

/**
 * Which of the two paths a launch takes, and what each produces (TASK-025b).
 *
 * The distinction under test is the one CLAUDE.md and TASK-051d insist on: an
 * EHR launch has already named the patient and must not be offered a search,
 * because searching there lets a provider start a visit against a different
 * patient than the chart in front of them. Only a launch that named nobody
 * searches.
 */

const LAUNCH_ID = 'launch-7';
const PROVIDER_ID = '22222222-2222-4222-8222-222222222222';

function context(overrides: Partial<LaunchContext> = {}): LaunchContext {
  return { patientId: 'p1', encounterId: 'e1', providerId: PROVIDER_ID, ...overrides };
}

function fhirReturning(
  launch: ApiResult<LaunchContext>,
  search: ApiResult<PatientSearchResults> = { ok: true, value: { matches: [], truncated: false } },
): FhirApi {
  return {
    getLaunchContext: async () => launch,
    searchPatients: async () => search,
  };
}

describe('subjectFromLaunch', () => {
  it('carries the chart entry through when the launch came from one', () => {
    // ehrEncounterId and launchId together are what fill the payer columns.
    expect(subjectFromLaunch(context())).toEqual({
      patientId: 'p1',
      providerId: PROVIDER_ID,
      ehrEncounterId: 'e1',
    });
  });

  it('omits the chart entry rather than sending a null for a standalone launch', () => {
    expect(subjectFromLaunch(context({ encounterId: null }))).toEqual({
      patientId: 'p1',
      providerId: PROVIDER_ID,
    });
  });

  it('is null when the launch named no patient', () => {
    expect(subjectFromLaunch(context({ patientId: null }))).toBeNull();
  });

  it('is null when the launch named no provider', () => {
    expect(subjectFromLaunch(context({ providerId: null }))).toBeNull();
  });
});

describe('resolveLaunch', () => {
  it('resolves an EHR launch without offering a search', async () => {
    const resolution = await resolveLaunch(fhirReturning({ ok: true, value: context() }), LAUNCH_ID);

    expect(resolution).toEqual({
      kind: 'resolved',
      subject: {
        patientId: 'p1',
        providerId: PROVIDER_ID,
        ehrEncounterId: 'e1',
        launchId: LAUNCH_ID,
      },
    });
  });

  it('sends a standalone launch to the search, carrying the provider', async () => {
    // The provider comes from the launch on both paths: a search finds a
    // patient and says nothing about who is treating them.
    const resolution = await resolveLaunch(
      fhirReturning({ ok: true, value: context({ patientId: null, encounterId: null }) }),
      LAUNCH_ID,
    );

    expect(resolution).toEqual({ kind: 'search', providerId: PROVIDER_ID });
  });

  it('refuses a launch with no provider instead of sending it to a search', async () => {
    // Searching would find a patient and still leave the visit unstartable, so
    // the refusal belongs here rather than after the provider has picked
    // someone.
    const resolution = await resolveLaunch(
      fhirReturning({ ok: true, value: context({ providerId: null }) }),
      LAUNCH_ID,
    );

    expect(resolution).toEqual({ kind: 'no-provider' });
  });

  it('reports a launch that could not be read', async () => {
    const failure = { kind: 'status', status: 404, code: 'FHIR_UNKNOWN_LAUNCH', message: 'x' } as const;

    const resolution = await resolveLaunch(fhirReturning({ ok: false, failure }), LAUNCH_ID);

    expect(resolution).toEqual({ kind: 'failed', failure });
  });
});

describe('subjectFromMatch', () => {
  it('carries the launch and no chart entry', () => {
    // A standalone launch corresponds to no chart entry, and inventing one
    // would file the note against a visit the EHR does not have.
    const subject = subjectFromMatch(
      {
        patientId: 'synthea-123',
        familyName: 'Sanchez',
        givenNames: ['Aurelio'],
        birthDate: '1962-04-17',
        gender: 'male',
      },
      PROVIDER_ID,
      LAUNCH_ID,
    );

    expect(subject).toEqual({
      patientId: 'synthea-123',
      providerId: PROVIDER_ID,
      launchId: LAUNCH_ID,
    });
    expect(subject.ehrEncounterId).toBeUndefined();
  });
});

describe('patientSelectionUnavailable', () => {
  it('still resolves to nothing, because a build with no launch identifies nobody', async () => {
    await expect(patientSelectionUnavailable()).resolves.toBeNull();
  });
});

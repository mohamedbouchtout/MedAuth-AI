/**
 * Deciding which patient a visit is about, before the visit starts (TASK-070).
 *
 * This screen exists so `SessionScreen` does not have to. Identifying a patient
 * is interactive in one of its two cases and instant in the other, and folding
 * that into the session screen would have meant a search box living inside a
 * component whose job is recording. What it hands on is a decided subject.
 *
 * **The two paths, and why the order is not a preference.** An EHR launch has
 * already named the patient, so this screen resolves and moves on without ever
 * showing a search box. Only a standalone launch — where nobody told us who is
 * in the room — reaches the search. Offering a search after an EHR launch would
 * let a provider start a visit against a different patient than the chart in
 * front of them, which is a silent, permanent error.
 *
 * **The order itself is `resolveLaunch` in `@medauth/fhir-client`, not logic in
 * this file.** TASK-070 was told to mirror `apps/mobile`'s arrangement rather
 * than re-derive it, and the strongest form of mirroring is calling the same
 * function: two screens each deciding when a search is appropriate is how one of
 * them eventually decides wrongly.
 *
 * **A candidate is shown with a date of birth.** Two people in one practice
 * share a name far more often than they share a name and a date of birth, and
 * this screen is the last point at which a wrong pick can be noticed by a human.
 *
 * Nothing here logs: the query is a patient's name and every result carries
 * patient identifiers.
 */

import {
  resolveLaunch,
  subjectFromMatch,
  type FhirApi,
  type PatientMatch,
  type VisitSubject,
} from '@medauth/fhir-client';
import type { ApiFailure } from '@medauth/session-client';
import { useCallback, useEffect, useRef, useState } from 'react';

import { fhirApi as defaultFhirApi } from '../api/fhirClient';

export const NO_LAUNCH_MESSAGE =
  'MedAuth AI is not signed in to an EHR, so it has no way to identify the patient. Sign in to the EHR and try again.';

export const NO_PROVIDER_MESSAGE =
  'This launch did not identify the provider, so a visit cannot be started. Launch MedAuth AI again from the EHR; if it keeps happening, the EHR is not supplying a verifiable practitioner.';

export const NO_MATCHES_MESSAGE = 'No patients matched that name.';

export const TRUNCATED_MESSAGE =
  'More patients matched than are shown. Narrow the search by adding a date of birth.';

type PickerState =
  | { kind: 'loading' }
  | { kind: 'failed'; message: string }
  | {
      kind: 'searching';
      providerId: string;
      matches: PatientMatch[] | null;
      truncated: boolean;
      busy: boolean;
      message: string | null;
    };

export interface PatientPickerProps {
  /** The SMART launch this app holds, or null when it holds none. */
  launchId: string | null;
  onResolved: (subject: VisitSubject) => void;
  fhir?: FhirApi;
}

function describe(failure: ApiFailure, prefix: string): string {
  return `${prefix} ${failure.message}`;
}

function patientLabel(match: PatientMatch): string {
  const name = [...match.givenNames, match.familyName].filter((part) => part !== null).join(' ');
  return name === '' ? match.patientId : name;
}

export function PatientPicker({ launchId, onResolved, fhir = defaultFhirApi }: PatientPickerProps) {
  const [state, setState] = useState<PickerState>(
    launchId === null ? { kind: 'failed', message: NO_LAUNCH_MESSAGE } : { kind: 'loading' },
  );
  const [query, setQuery] = useState('');
  const [birthDate, setBirthDate] = useState('');

  /**
   * Held in a ref so the effect below depends only on the launch.
   *
   * `onResolved` is a callback a caller may well recreate on every render, and
   * with it in the dependency array the launch would be re-read on every
   * keystroke in the search box — restarting the resolution, discarding the
   * matches already on screen, and issuing one request per character. That is a
   * defect `apps/mobile`'s picker actually shipped and had caught by its first
   * test, so it is written down here rather than rediscovered.
   */
  const onResolvedRef = useRef(onResolved);
  useEffect(() => {
    onResolvedRef.current = onResolved;
  }, [onResolved]);

  useEffect(() => {
    if (launchId === null) {
      return;
    }
    let cancelled = false;

    void (async () => {
      const resolution = await resolveLaunch(fhir, launchId);
      if (cancelled) {
        return;
      }
      switch (resolution.kind) {
        case 'resolved':
          // An EHR launch: the patient is the one on the chart, and no search is
          // offered at all.
          onResolvedRef.current(resolution.subject);
          return;
        case 'search':
          setState({
            kind: 'searching',
            providerId: resolution.providerId,
            matches: null,
            truncated: false,
            busy: false,
            message: null,
          });
          return;
        case 'no-provider':
          setState({ kind: 'failed', message: NO_PROVIDER_MESSAGE });
          return;
        default:
          setState({
            kind: 'failed',
            message: describe(resolution.failure, 'The launch could not be read.'),
          });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [fhir, launchId]);

  const onSearch = useCallback(async () => {
    if (state.kind !== 'searching' || query.trim() === '' || launchId === null) {
      return;
    }
    const current = state;
    setState({ ...current, busy: true, message: null });

    const result = await fhir.searchPatients(launchId, query.trim(), birthDate.trim());
    if (!result.ok) {
      setState({
        ...current,
        busy: false,
        message: describe(result.failure, 'The search could not be run.'),
      });
      return;
    }
    setState({
      ...current,
      busy: false,
      matches: result.value.matches,
      truncated: result.value.truncated,
      message: null,
    });
  }, [birthDate, fhir, launchId, query, state]);

  const onPick = useCallback(
    (match: PatientMatch) => {
      if (state.kind !== 'searching' || launchId === null) {
        return;
      }
      onResolvedRef.current(subjectFromMatch(match, state.providerId, launchId));
    },
    [launchId, state],
  );

  if (state.kind === 'loading') {
    return (
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6" data-testid="patient-picker">
        <h1 className="text-lg font-semibold text-slate-900">MedAuth AI</h1>
        <p className="text-sm text-slate-700">Reading the launch…</p>
      </main>
    );
  }

  if (state.kind === 'failed') {
    return (
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6" data-testid="patient-picker">
        <h1 className="text-lg font-semibold text-slate-900">MedAuth AI</h1>
        <p role="alert" className="text-sm font-semibold text-red-800" data-testid="picker-failed">
          {state.message}
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6" data-testid="patient-picker">
      <h1 className="text-lg font-semibold text-slate-900">Find the patient</h1>

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          void onSearch();
        }}
      >
        <label className="flex flex-col gap-1 text-sm text-slate-700">
          Name
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            data-testid="patient-query"
            className="rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>

        <label className="flex flex-col gap-1 text-sm text-slate-700">
          Date of birth
          {/*
            A plain text field rather than `type="date"`: this is passed straight
            through to the FHIR `birthdate` parameter, which is an ISO date, and a
            native picker's locale-formatted value is not one.
          */}
          <input
            type="text"
            inputMode="numeric"
            placeholder="YYYY-MM-DD"
            value={birthDate}
            onChange={(event) => setBirthDate(event.target.value)}
            data-testid="patient-birth-date"
            className="rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>

        <button
          type="submit"
          disabled={state.busy || query.trim() === ''}
          data-testid="patient-search"
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-semibold text-white disabled:opacity-60"
        >
          {state.busy ? 'Searching…' : 'Search'}
        </button>
      </form>

      {state.message !== null && (
        <p role="alert" className="text-sm font-semibold text-red-800" data-testid="search-failed">
          {state.message}
        </p>
      )}

      {state.truncated && (
        // Rendered rather than ignored: a provider shown twenty of two hundred
        // Smiths and not told so concludes the patient they want is absent.
        <p className="text-sm text-slate-700" data-testid="search-truncated">
          {TRUNCATED_MESSAGE}
        </p>
      )}

      {state.matches !== null &&
        (state.matches.length === 0 ? (
          <p className="text-sm text-slate-700" data-testid="search-empty">
            {NO_MATCHES_MESSAGE}
          </p>
        ) : (
          <ul className="flex flex-col gap-2" data-testid="search-results">
            {state.matches.map((match) => (
              <li key={match.patientId}>
                <button
                  type="button"
                  onClick={() => onPick(match)}
                  className="w-full rounded border border-slate-300 p-3 text-left text-sm hover:bg-slate-50"
                >
                  <span className="font-semibold text-slate-900">{patientLabel(match)}</span>
                  {/*
                    The date of birth is what distinguishes two people with one
                    name, so it is shown even when the EHR did not supply one —
                    "not recorded" is information a provider needs in order to
                    know they cannot tell these candidates apart here.
                  */}
                  <span className="ml-2 text-slate-600">
                    {match.birthDate ?? 'date of birth not recorded'}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ))}
    </main>
  );
}

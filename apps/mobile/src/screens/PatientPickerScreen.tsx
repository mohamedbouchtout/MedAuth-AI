/**
 * Deciding which patient a visit is about, before the visit starts (TASK-025b).
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
 * front of them, which is a silent, permanent error; see CLAUDE.md and
 * TASK-051d.
 *
 * **A candidate is shown with a date of birth.** Two people in one practice
 * share a name far more often than they share a name and a date of birth, and
 * this screen is the last point at which a wrong pick can be noticed by a human.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import type { ApiFailure } from '@medauth/session-client';

import { fhirApi as defaultFhirApi } from '../api/fhirClient';
import type { FhirApi, PatientMatch } from '../api/fhir';
import {
  resolveLaunch,
  subjectFromMatch,
  type VisitSubject,
} from '../session/patientSource';

export const NO_LAUNCH_MESSAGE =
  'MedAuth AI cannot start a visit yet: this build cannot obtain a SMART launch, so it has no way to identify the patient. Launching from the EHR arrives in TASK-025c.';

export const NO_PROVIDER_MESSAGE =
  'This launch did not identify the provider, so a visit cannot be started. Launch MedAuth AI again from the EHR; if it keeps happening, the EHR is not supplying a verifiable practitioner.';

export const NO_MATCHES_MESSAGE = 'No patients matched that name.';

export const TRUNCATED_MESSAGE =
  'More patients matched than are shown. Narrow the search by adding a date of birth.';

interface Loading {
  kind: 'loading';
}
interface Failed {
  kind: 'failed';
  message: string;
}
interface Searching {
  kind: 'searching';
  providerId: string;
  matches: PatientMatch[] | null;
  truncated: boolean;
  busy: boolean;
  message: string | null;
}

type PickerState = Loading | Failed | Searching;

export interface PatientPickerScreenProps {
  /**
   * The SMART launch this app holds, or null when it holds none.
   *
   * Null is every build until TASK-025c: both routes behind this screen are
   * keyed on a launch because both need its EHR access token.
   */
  launchId: string | null;
  /** Called once the subject is decided. The visit starts after this. */
  onResolved: (subject: VisitSubject) => void;
  fhir?: FhirApi;
}

function describe(failure: ApiFailure, prefix: string): string {
  return `${prefix} ${failure.message}`;
}

function displayName(match: PatientMatch): string {
  const given = match.givenNames.join(' ');
  const full = [given, match.familyName].filter((part) => part).join(' ');
  // An EHR may hold a patient with no name recorded at all. Showing the id is
  // worse than nothing only if it is unlabelled, so it is labelled.
  return full === '' ? `Unnamed patient (${match.patientId})` : full;
}

export function PatientPickerScreen({
  launchId,
  onResolved,
  fhir = defaultFhirApi,
}: PatientPickerScreenProps): React.JSX.Element {
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
   * matches already on screen, and issuing one request per character.
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
      <View style={styles.container} testID="patient-picker">
        <Text style={styles.title}>MedAuth AI</Text>
        <ActivityIndicator accessibilityLabel="Reading the launch" />
        <Text style={styles.body}>Reading the launch…</Text>
      </View>
    );
  }

  if (state.kind === 'failed') {
    return (
      <View style={styles.container} testID="patient-picker">
        <Text style={styles.title}>MedAuth AI</Text>
        <View style={styles.block} accessibilityRole="alert" testID="picker-error">
          <Text style={styles.errorTitle}>A visit cannot be started.</Text>
          <Text style={styles.body}>{state.message}</Text>
        </View>
      </View>
    );
  }

  return (
    <View style={styles.container} testID="patient-picker">
      <Text style={styles.title}>MedAuth AI</Text>
      <Text style={styles.body}>
        This launch did not name a patient. Search for the one you are seeing.
      </Text>

      <TextInput
        style={styles.input}
        value={query}
        onChangeText={setQuery}
        placeholder="Patient name"
        accessibilityLabel="Patient name"
        testID="patient-query"
        autoCorrect={false}
      />
      <TextInput
        style={styles.input}
        value={birthDate}
        onChangeText={setBirthDate}
        placeholder="Date of birth (YYYY-MM-DD, optional)"
        accessibilityLabel="Date of birth"
        testID="patient-birth-date"
        autoCorrect={false}
      />
      <Pressable
        accessibilityRole="button"
        style={styles.action}
        onPress={() => {
          void onSearch();
        }}
        testID="search-patients"
      >
        <Text style={styles.actionLabel}>Search</Text>
      </Pressable>

      {state.busy ? <ActivityIndicator accessibilityLabel="Searching" /> : null}
      {state.message === null ? null : (
        <Text style={styles.body} accessibilityRole="alert" testID="search-error">
          {state.message}
        </Text>
      )}

      {state.matches !== null && state.matches.length === 0 ? (
        <Text style={styles.body} testID="no-matches">
          {NO_MATCHES_MESSAGE}
        </Text>
      ) : null}

      {/*
        The truncation notice is not decoration. A provider shown a short list
        and not told it is short concludes the patient is not in the system.
      */}
      {state.truncated ? (
        <Text style={styles.body} testID="truncated-notice">
          {TRUNCATED_MESSAGE}
        </Text>
      ) : null}

      {(state.matches ?? []).map((match) => (
        <Pressable
          key={match.patientId}
          accessibilityRole="button"
          style={styles.match}
          onPress={() => onPick(match)}
          testID={`patient-${match.patientId}`}
        >
          <Text style={styles.matchName}>{displayName(match)}</Text>
          <Text style={styles.matchDetail}>
            {[match.birthDate ?? 'no date of birth recorded', match.gender]
              .filter((part) => part)
              .join(' · ')}
          </Text>
        </Pressable>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, gap: 12, justifyContent: 'center', padding: 24 },
  block: { gap: 8 },
  title: { fontSize: 24, fontWeight: '600' },
  body: { fontSize: 16 },
  errorTitle: { fontSize: 18, fontWeight: '600' },
  input: { borderColor: '#999', borderRadius: 6, borderWidth: 1, fontSize: 16, padding: 12 },
  action: { alignItems: 'center', backgroundColor: '#1f6feb', borderRadius: 6, padding: 14 },
  actionLabel: { color: '#fff', fontSize: 16, fontWeight: '600' },
  match: { borderColor: '#ddd', borderRadius: 6, borderWidth: 1, gap: 4, padding: 12 },
  matchName: { fontSize: 16, fontWeight: '600' },
  matchDetail: { color: '#555', fontSize: 14 },
});

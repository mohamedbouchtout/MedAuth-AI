/**
 * The two screens a visit passes through, in order (TASK-070).
 *
 * `PatientPicker` decides *who* the visit is about — from the launch context
 * after an EHR launch, or from a search after a standalone one — and
 * `SessionScreen` then records it. The order is the substance rather than
 * navigation: the session screen takes a decided subject, and this is where it
 * is decided.
 *
 * It is a component rather than inline in `App` so the whole path can be driven
 * in a test with a launch injected. The launch itself comes from `App`, which
 * redeems the claim code before this component is rendered at all.
 */

import { fixedSource, type FhirApi, type VisitSubject } from '@medauth/fhir-client';
import { useCallback, useState } from 'react';

import type { SessionsApi } from '../api/sessions';
import type { CompletedVisit } from '../session/completedVisit';

import { PatientPicker } from './PatientPicker';
import { SessionScreen } from './SessionScreen';

export interface VisitFlowProps {
  /** The SMART launch this app holds, or null when it holds none. */
  launchId: string | null;
  onCompleted?: (visit: CompletedVisit) => void;
  fhir?: FhirApi;
  sessions?: SessionsApi;
}

export function VisitFlow({ launchId, onCompleted, fhir, sessions }: VisitFlowProps) {
  const [subject, setSubject] = useState<VisitSubject | null>(null);
  const onResolved = useCallback((resolved: VisitSubject) => setSubject(resolved), []);

  // Spread rather than pass through: `exactOptionalPropertyTypes` is on, so an
  // explicit `undefined` is not the same as an absent prop and would defeat the
  // default each screen declares.
  if (subject === null) {
    return (
      <PatientPicker
        launchId={launchId}
        onResolved={onResolved}
        {...(fhir === undefined ? {} : { fhir })}
      />
    );
  }

  return (
    <SessionScreen
      patientSource={fixedSource(subject)}
      {...(onCompleted === undefined ? {} : { onCompleted })}
      {...(sessions === undefined ? {} : { sessions })}
    />
  );
}

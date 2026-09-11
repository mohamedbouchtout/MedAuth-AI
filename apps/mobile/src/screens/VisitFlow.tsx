/**
 * The two screens a visit passes through, in order (TASK-025b).
 *
 * `PatientPickerScreen` decides *who* the visit is about — from the launch
 * context after an EHR launch, or from a search after a standalone one — and
 * `SessionScreen` then records it. The order is the substance rather than
 * navigation: TASK-025 built the session screen against a seam precisely because
 * neither identifier had a source, and this is the source.
 *
 * It is a component rather than inline in `App.tsx` so the whole path can be
 * driven in a test with a launch injected. The launch itself comes from
 * `LaunchFlow`, which obtains one before this component is rendered at all
 * (TASK-025c); until then nothing on this platform could produce one.
 */

import { useCallback, useState } from 'react';

import { fixedSource, type FhirApi, type VisitSubject } from '@medauth/fhir-client';
import type { SessionsApi } from '../api/sessions';


import { PatientPickerScreen } from './PatientPickerScreen';
import { SessionScreen } from './SessionScreen';

export interface VisitFlowProps {
  /** The SMART launch this app holds, or null when it holds none. */
  launchId: string | null;
  fhir?: FhirApi;
  sessions?: SessionsApi;
}

export function VisitFlow({ launchId, fhir, sessions }: VisitFlowProps): React.JSX.Element {
  const [subject, setSubject] = useState<VisitSubject | null>(null);
  const onResolved = useCallback((resolved: VisitSubject) => setSubject(resolved), []);

  // Spread rather than pass through: `exactOptionalPropertyTypes` is on, so an
  // explicit `undefined` is not the same as an absent prop and would defeat the
  // default each screen declares.
  if (subject === null) {
    return (
      <PatientPickerScreen
        launchId={launchId}
        onResolved={onResolved}
        {...(fhir === undefined ? {} : { fhir })}
      />
    );
  }
  return (
    <SessionScreen
      patientSource={fixedSource(subject)}
      {...(sessions === undefined ? {} : { sessions })}
    />
  );
}

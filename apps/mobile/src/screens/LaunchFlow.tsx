/**
 * The three screens a visit passes through, in order (TASK-025c).
 *
 * `LaunchScreen` obtains the SMART launch, `PatientPickerScreen` decides who
 * the visit is about, and `SessionScreen` records it. The order is the
 * substance rather than navigation: each screen needs what the one before it
 * produced, and the launch is what TASK-025b left missing — every route that
 * names a patient is keyed on it because every one spends its EHR access token.
 *
 * **The launch is held here, in memory, for the life of the app process.** It
 * resolves to an EHR access token, so it is a credential by this repository's
 * own definition and never reaches disk; `expo-secure-store` would be the floor
 * if it ever had to persist, and it does not, because a launch outlives neither
 * the working day nor `SMART_LAUNCH_RECORD_TTL_SECONDS`. Losing it when the
 * process dies is correct rather than a gap: the launch behind it is gone too.
 *
 * It is a component rather than inline in `App.tsx` so the whole path can be
 * driven in a test with a fake browser and a fake service, which is the only
 * way either launch type is testable — nothing in Jest performs an OAuth
 * redirect.
 */

import { useCallback, useState } from 'react';

import type { FhirApi } from '../api/fhir';
import type { LaunchApi, LaunchRequest, LaunchSession } from '../api/launch';
import type { SessionsApi } from '../api/sessions';
import type { AuthSessionOpener } from '../launch/smartLaunch';

import { LaunchScreen } from './LaunchScreen';
import { VisitFlow } from './VisitFlow';

export interface LaunchFlowProps {
  /** The EHR-initiated launch this app was opened with, when it was. */
  inbound?: LaunchRequest | null;
  iss?: string;
  returnUri?: string;
  launch?: LaunchApi;
  open?: AuthSessionOpener;
  fhir?: FhirApi;
  sessions?: SessionsApi;
}

export function LaunchFlow({
  inbound = null,
  iss,
  returnUri,
  launch,
  open,
  fhir,
  sessions,
}: LaunchFlowProps): React.JSX.Element {
  const [session, setSession] = useState<LaunchSession | null>(null);
  const onLaunched = useCallback((launched: LaunchSession) => setSession(launched), []);

  // Spread rather than pass through: `exactOptionalPropertyTypes` is on, so an
  // explicit `undefined` is not the same as an absent prop and would defeat the
  // default each screen declares.
  if (session === null) {
    return (
      <LaunchScreen
        onLaunched={onLaunched}
        inbound={inbound}
        {...(iss === undefined ? {} : { iss })}
        {...(returnUri === undefined ? {} : { returnUri })}
        {...(launch === undefined ? {} : { api: launch })}
        {...(open === undefined ? {} : { open })}
      />
    );
  }

  return (
    <VisitFlow
      launchId={session.launchId}
      {...(fhir === undefined ? {} : { fhir })}
      {...(sessions === undefined ? {} : { sessions })}
    />
  );
}

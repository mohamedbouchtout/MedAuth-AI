/**
 * This app's binding of the fhir-integration clients.
 *
 * The clients themselves are `@medauth/fhir-client`, which this app and
 * `apps/mobile` both import — see that package's note for why one definition of
 * "when is a patient search the right question" is worth more than two tidy
 * local copies. What stays here is what is genuinely this app's: which host to
 * talk to, and which delivery this platform asks a launch to come back through.
 *
 * `FHIR_INTEGRATION_URL` is a third service on a third port. It is not
 * `API_BASE_URL` (track-a-clinical, session lifecycle) and not
 * `TRACK_B_RAG_URL` (nudge acknowledgement) — see `../config`.
 *
 * **`web` is the delivery, fixed here rather than passed per launch.** It is
 * what makes the callback redirect to `SMART_WEB_RETURN_URL` carrying a claim
 * code instead of rendering `{launch_id, ehr_type, expires_in}` as JSON into the
 * browser the EHR redirected — which is a correct answer for a
 * service-to-service caller and no answer at all for this app. Which delivery is
 * right is a property of the platform, so it is stated once, at the binding.
 */

import { createFhirApi, createLaunchApi } from '@medauth/fhir-client';

import { FHIR_INTEGRATION_URL } from '../config';

export type {
  FhirApi,
  LaunchApi,
  LaunchContext,
  LaunchRequest,
  LaunchSession,
  PatientMatch,
  PatientSearchResults,
  VisitSubject,
} from '@medauth/fhir-client';

/** The client the patient picker uses when none is injected. */
export const fhirApi = createFhirApi(FHIR_INTEGRATION_URL);

/** The client the launch flow uses when none is injected. */
export const launchApi = createLaunchApi(FHIR_INTEGRATION_URL, 'web');

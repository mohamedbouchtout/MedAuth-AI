/**
 * This app's binding of the fhir-integration client.
 *
 * The client itself is `./fhir`; what stays here is the one thing that is
 * genuinely this app's — which host to talk to. Same arrangement as
 * `./sessions` and `./nudges`, which bind the two shared packages.
 *
 * `FHIR_INTEGRATION_URL` is a third service on a third port, and is not
 * interchangeable with either of the other two HTTP origins. See `../config`.
 */

import { FHIR_INTEGRATION_URL } from '../config';

import { createFhirApi } from './fhir';

export type { FhirApi, LaunchContext, PatientMatch, PatientSearchResults } from './fhir';

/** The client the patient picker uses when none is injected. */
export const fhirApi = createFhirApi(FHIR_INTEGRATION_URL);

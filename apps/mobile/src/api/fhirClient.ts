/**
 * This app's binding of the fhir-integration client.
 *
 * The client itself is `@medauth/fhir-client`, which this app and `apps/web`
 * both import — it lived here until TASK-070 made the web session screen its
 * second consumer. What stays is the one thing that is genuinely this app's:
 * which host to talk to. Same arrangement as `./sessions`, `./nudges` and
 * `./launchClient`, which bind the other shared packages.
 *
 * `FHIR_INTEGRATION_URL` is a third service on a third port, and is not
 * interchangeable with either of the other two HTTP origins. See `../config`.
 */

import { createFhirApi } from '@medauth/fhir-client';

import { FHIR_INTEGRATION_URL } from '../config';

export type {
  FhirApi,
  LaunchContext,
  PatientMatch,
  PatientSearchResults,
} from '@medauth/fhir-client';

/** The client the patient picker uses when none is injected. */
export const fhirApi = createFhirApi(FHIR_INTEGRATION_URL);

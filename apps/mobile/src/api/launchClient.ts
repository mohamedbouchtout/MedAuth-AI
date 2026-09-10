/**
 * This app's binding of the launch client.
 *
 * The client itself is `./launch`; what stays here is which host to talk to,
 * the same arrangement as `./fhirClient`, `./sessions` and `./nudges`. The
 * launch routes belong to fhir-integration, so this is `FHIR_INTEGRATION_URL`
 * — the same origin `./fhirClient` uses, because it is the same service, and
 * not a fourth variable naming the same host twice.
 */

import { FHIR_INTEGRATION_URL } from '../config';

import { createLaunchApi } from './launch';

export type { LaunchApi, LaunchRequest, LaunchSession } from './launch';

/** The client the launch screen uses when none is injected. */
export const launchApi = createLaunchApi(FHIR_INTEGRATION_URL);

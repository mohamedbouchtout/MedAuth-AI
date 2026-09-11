/**
 * This app's binding of the launch client.
 *
 * The client itself is `@medauth/fhir-client`, shared with `apps/web` since
 * TASK-070; what stays here is which host to talk to and which delivery this
 * platform asks for. Same arrangement as `./fhirClient`, `./sessions` and
 * `./nudges`. The launch routes belong to fhir-integration, so this is
 * `FHIR_INTEGRATION_URL` — the same origin `./fhirClient` uses, because it is
 * the same service, and not a fourth variable naming the same host twice.
 *
 * **`mobile` is the delivery, fixed here rather than passed per launch.** It is
 * what makes the callback redirect to `SMART_MOBILE_RETURN_URI` with a claim
 * code instead of rendering JSON into a browser this app cannot read. Which
 * delivery is right is a property of the platform, so the app states it once at
 * the binding — `apps/web` states `web` in its own.
 */

import { createLaunchApi } from '@medauth/fhir-client';

import { FHIR_INTEGRATION_URL } from '../config';

export type { LaunchApi, LaunchRequest, LaunchSession } from '@medauth/fhir-client';

/** The client the launch screen uses when none is injected. */
export const launchApi = createLaunchApi(FHIR_INTEGRATION_URL, 'mobile');

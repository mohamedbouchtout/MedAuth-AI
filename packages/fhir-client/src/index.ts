/**
 * The fhir-integration client, shared by `apps/web` and `apps/mobile`.
 *
 * **Why this is a package.** It was `apps/mobile/src/api/fhir.ts`,
 * `src/api/launch.ts` and `src/session/patientSource.ts` until TASK-070, when
 * `apps/web`'s session screen became the second client needing to obtain a SMART
 * launch and then ask it which patient a visit is about. That is the same
 * trigger, and the same argument, that produced `packages/session-client` when
 * the web nudge overlay needed mobile's re-mint path and `packages/nudge-client`
 * when mobile needed the web app's payload parse — and TASK-025b said in writing
 * that this extraction was TASK-070's to make, rather than guessing at the shape
 * a second consumer would need before one existed.
 *
 * The hazard here is specific rather than stylistic. `patientSource` encodes an
 * *order*: an EHR launch has already named the patient, so a search is the right
 * question only for a standalone launch. An app that re-derived that order and
 * got it backwards would offer a provider a patient search while the chart in
 * front of them names someone else, and every layer below — the encounter, the
 * note, the prior-auth bundle — would record the choice without complaint.
 *
 * **Scope note:** the browser-reachable routes of `fhir-integration` and the
 * types they exchange — obtaining a launch (`GET /fhir/launch`,
 * `POST /fhir/launch/claim`), and the two routes keyed on one that identify a
 * patient (`GET /fhir/launch-context`, `GET /fhir/patient/search`). It is not a
 * general HTTP client and holds no other service's routes: session lifecycle is
 * `@medauth/session-client` and nudge acknowledgement is `@medauth/nudge-client`.
 *
 * It holds no UI and no platform API either. How a launch is *driven* differs by
 * platform in a way that cannot be shared — mobile opens a system browser and
 * waits for the OS to route a custom scheme back, web navigates the page it is
 * running in and reads the redirect off its own URL — so each app keeps that
 * half and both spend the resulting claim code through `redeemClaim` here.
 */

export { createFhirApi, LAUNCH_ID_HEADER } from './fhir';
export type { FhirApi, LaunchContext, PatientMatch, PatientSearchResults } from './fhir';

export { createLaunchApi } from './launch';
export type { LaunchApi, LaunchDelivery, LaunchRequest, LaunchSession } from './launch';

export { fixedSource, resolveLaunch, subjectFromLaunch, subjectFromMatch } from './patientSource';
export type { LaunchResolution, PatientSource, VisitSubject } from './patientSource';

/**
 * Where the patient and provider for a visit come from.
 *
 * TASK-025 built this as a seam returning null, because nothing on this platform
 * could identify either. TASK-025b fills it, and the shape of the answer is two
 * paths that must not be confused:
 *
 * - **An EHR launch already named the patient.** `GET /fhir/launch-context`
 *   returns them, along with the chart entry and the resolved provider. Take
 *   that answer. Offering a search here would let a provider start a visit
 *   against a different patient than the chart in front of them.
 * - **A standalone launch named nobody.** Only then is
 *   `GET /fhir/patient/search` the right question, and the provider picks from
 *   what it returns.
 *
 * The type is still a plain thunk, which is what keeps the search interaction
 * out of `SessionScreen`: by the time that screen calls this, the subject is
 * decided. `PatientPickerScreen` is what decides it in the standalone case.
 *
 * **A hardcoded patient id would still be wrong**, for the reason TASK-025 gave:
 * it is indistinguishable from a real one at runtime, and the failure it
 * produces — an encounter, a SOAP note and a prior-auth bundle filed against the
 * wrong patient — is silent at every layer that would otherwise catch it.
 */

import type { ApiFailure, ApiResult } from '@medauth/session-client';

import type { FhirApi, LaunchContext, PatientMatch } from '../api/fhir';

export interface VisitSubject {
  /** FHIR patient id; the wire field is `patient_id` (see CLAUDE.md). */
  patientId: string;
  /** The provider's UUID, as recorded on the `encounters` row. */
  providerId: string;
  /**
   * The chart entry this visit corresponds to, when the launch came from one.
   *
   * Passed through to `POST /sessions/start` with `launchId`, because the two
   * together are what let the service fill the encounter's payer columns.
   * Absent for a standalone launch, which has no chart entry to name.
   */
  ehrEncounterId?: string;
  /** The SMART launch this visit was started under. Never a `sessionId`. */
  launchId?: string;
}

/** Resolves the subject of a visit, or null when none can be determined. */
export type PatientSource = () => Promise<VisitSubject | null>;

/**
 * The production source before a launch exists: there isn't one.
 *
 * Still wired in `App.tsx` when the app holds no `launch_id`, which is every
 * build until **TASK-025c** performs a SMART launch from this platform. Both
 * routes below are keyed on a launch, because both need its EHR access token,
 * so without one this app genuinely cannot identify anybody — and showing a
 * provider that a visit cannot start is the honest version of that.
 */
export const patientSelectionUnavailable: PatientSource = async () => null;

/**
 * Turn a launch context into a subject, or null when it named no patient.
 *
 * Null here is not an error and does not mean the launch is bad: a standalone
 * launch legitimately carries no patient, and the caller's next move is to
 * search. It is the same distinction the service draws by answering 200 with a
 * null `patient_id` rather than 404.
 *
 * A launch that named a patient but no provider is also null, and that is the
 * conservative half of this function. `provider_id` is absent when the EHR did
 * not say who launched us or could not prove it, and `POST /sessions/start`
 * requires one — so the alternatives would be inventing a provider or letting
 * the request fail at the service with a validation error the provider cannot
 * act on. Searching will not produce a provider either, so the caller reports it
 * rather than falling through.
 */
export function subjectFromLaunch(context: LaunchContext): VisitSubject | null {
  if (context.patientId === null || context.providerId === null) {
    return null;
  }
  return {
    patientId: context.patientId,
    providerId: context.providerId,
    ...(context.encounterId === null ? {} : { ehrEncounterId: context.encounterId }),
  };
}

/** What resolving a launch produced, and what the caller should do next. */
export type LaunchResolution =
  /** The EHR named the patient. Start the visit. */
  | { kind: 'resolved'; subject: VisitSubject }
  /** A standalone launch, or one with no provider. `providerId` gates the search. */
  | { kind: 'search'; providerId: string }
  /** The launch itself could not be read, or named no provider at all. */
  | { kind: 'failed'; failure: ApiFailure }
  /** No provider identity, so no visit — see `NO_PROVIDER_MESSAGE`. */
  | { kind: 'no-provider' };

/**
 * Read the launch and decide which of the two paths this visit takes.
 *
 * The provider comes from the launch in **both** paths. A search finds a
 * patient and says nothing about who is treating them, so a standalone launch
 * whose actor could not be verified cannot start a visit however well the search
 * works — which is why that case is answered here rather than discovered after
 * the provider has typed a name and picked someone.
 */
export async function resolveLaunch(
  fhir: FhirApi,
  launchId: string,
): Promise<LaunchResolution> {
  const result = await fhir.getLaunchContext(launchId);
  if (!result.ok) {
    return { kind: 'failed', failure: result.failure };
  }

  const context = result.value;
  if (context.providerId === null) {
    return { kind: 'no-provider' };
  }

  const subject = subjectFromLaunch(context);
  return subject === null
    ? { kind: 'search', providerId: context.providerId }
    : { kind: 'resolved', subject: { ...subject, launchId } };
}

/**
 * Build the subject for a patient the provider picked out of a search.
 *
 * No `ehrEncounterId`: a standalone launch corresponds to no chart entry, and
 * inventing one would file the note against a visit the EHR does not have. The
 * encounter's payer columns stay NULL as a result, which the policy dispatcher
 * already reports per procedure — that is the honest cost of starting a visit
 * outside a chart, and it is not something a guess should paper over.
 */
export function subjectFromMatch(
  match: PatientMatch,
  providerId: string,
  launchId: string,
): VisitSubject {
  return { patientId: match.patientId, providerId, launchId };
}

/** A source that answers with a subject already decided. */
export function fixedSource(subject: VisitSubject): PatientSource {
  return async () => subject;
}

/** Re-exported so callers do not need a second import for one type. */
export type { ApiResult };

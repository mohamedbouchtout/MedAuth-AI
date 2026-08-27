/**
 * The seam where the patient and provider for a visit come from.
 *
 * `POST /sessions/start` requires a `patient_id` and a `provider_id` UUID, and
 * nothing on this platform can supply either today: fhir-integration exposes no
 * patient search route (TASK-052 defines the context and encounter reads only),
 * and provider authentication does not exist anywhere in this repository before
 * SMART on FHIR in Phase 5. **TASK-025b** fills this in on both halves.
 *
 * It is a seam rather than a placeholder value on purpose. A hardcoded patient
 * id would be indistinguishable from a real one at runtime, and the failure it
 * produces — an encounter, a SOAP note and a prior-auth bundle filed against the
 * wrong patient — is silent at every layer that would otherwise catch it.
 * Returning null instead makes "we cannot start a visit yet" a state the screen
 * shows a provider, which is the honest version of the same fact.
 */

export interface VisitSubject {
  /** FHIR patient id; the wire field is `patient_id` (see CLAUDE.md). */
  patientId: string;
  /** The provider's UUID, as recorded on the `encounters` row. */
  providerId: string;
}

/** Resolves the subject of a visit, or null when none can be determined. */
export type PatientSource = () => Promise<VisitSubject | null>;

/**
 * The production source until TASK-025b lands: there isn't one.
 *
 * Wiring this into `App.tsx` is what makes the statement in TASK-025 true —
 * that no build of `apps/mobile` can start a real encounter yet — rather than
 * merely intended.
 */
export const patientSelectionUnavailable: PatientSource = async () => null;

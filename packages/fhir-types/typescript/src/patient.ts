/**
 * The FHIR R4 Patient resource, mirroring `src/fhir_types/patient.py`.
 *
 * Every property on this resource is PHI. Nothing here belongs in a console log,
 * an error message, or a client-side analytics event.
 */

import type { DomainResource } from './base.js';
import type { AdministrativeGender } from './codes.js';
import type {
  Address,
  CodeableConcept,
  ContactPoint,
  HumanName,
  Identifier,
  Reference,
} from './datatypes.js';

/** A language the patient can communicate in. */
export interface PatientCommunication {
  /** The language, coded per BCP-47. */
  readonly language: CodeableConcept;
  /** True when this is the patient's preferred language. */
  readonly preferred?: boolean;
}

/**
 * Demographics and other administrative information about an individual.
 *
 * `deceased` is a FHIR choice element: a server sends either `deceasedBoolean` or
 * `deceasedDateTime`, never both. Both are optional here, so a caller checks
 * whichever arrived.
 */
export interface Patient extends DomainResource {
  readonly resourceType: 'Patient';
  /** Business identifiers, including the MRN. */
  readonly identifier?: readonly Identifier[];
  /** Whether the record is in active use. */
  readonly active?: boolean;
  /** Names by which the patient is known. */
  readonly name?: readonly HumanName[];
  /** Contact details. */
  readonly telecom?: readonly ContactPoint[];
  /** Administrative gender, used for record matching — this is not a statement
   * about the patient's gender identity, which FHIR carries as an extension
   * rather than in this element. */
  readonly gender?: AdministrativeGender;
  /** Date of birth, as a `date` string. */
  readonly birthDate?: string;
  /** Whether the patient is deceased, when no date is known. */
  readonly deceasedBoolean?: boolean;
  /** Date and time of death, when known. */
  readonly deceasedDateTime?: string;
  /** Addresses for the patient. This is *not* what selects the payer policy set
   * — an earlier version of this comment said it was, written before anyone read
   * what the policy documents say about their own applicability. They say the
   * site of care, so the `state` segment of the RAG cache key comes from the
   * encounter's `Location`/`Organization` address instead (TASK-052b). The state
   * here is compared against that one and a disagreement is logged, so a patient
   * treated out of state is visible rather than silent. */
  readonly address?: readonly Address[];
  /** Marital or civil status. */
  readonly maritalStatus?: CodeableConcept;
  /** Languages the patient can communicate in. */
  readonly communication?: readonly PatientCommunication[];
  /** The patient's primary care providers. */
  readonly generalPractitioner?: readonly Reference[];
  /** Organization that is custodian of the record. */
  readonly managingOrganization?: Reference;
}

/**
 * The FHIR R4 MedicationRequest resource, mirroring
 * `src/fhir_types/medication_request.py`.
 *
 * Medication orders are a prior authorization trigger in their own right —
 * biologics, specialty infusions and chemotherapy are authorized off the drug, not
 * a procedure code.
 */

import type { DomainResource } from './base.js';
import type {
  MedicationRequestIntent,
  MedicationRequestStatus,
  RequestPriority,
} from './codes.js';
import type {
  Annotation,
  CodeableConcept,
  Identifier,
  Quantity,
  Reference,
} from './datatypes.js';

/** The amount of medication administered per dose. */
export interface DosageDoseAndRate {
  /** Whether this is the ordered, calculated or adjusted amount. */
  readonly type?: CodeableConcept;
  /** Amount per administration. */
  readonly doseQuantity?: Quantity;
  /** Amount per unit of time, for infusions. */
  readonly rateQuantity?: Quantity;
}

/**
 * How the medication is to be taken.
 *
 * `timing` is not modelled: FHIR's Timing datatype is large and no code in this
 * project reads it. Modelling it later is additive rather than breaking.
 */
export interface Dosage {
  /** Order in which to apply multiple dosage instructions. */
  readonly sequence?: number;
  /** Free-text dosing instructions. PHI. */
  readonly text?: string;
  /** Supplemental coded instructions. */
  readonly additionalInstruction?: readonly CodeableConcept[];
  /** Instructions in terms the patient can act on. PHI. */
  readonly patientInstruction?: string;
  /** Whether the medication is taken only as needed. */
  readonly asNeededBoolean?: boolean;
  /** Body site of administration. */
  readonly site?: CodeableConcept;
  /** How the medication enters the body. */
  readonly route?: CodeableConcept;
  /** Technique of administration. */
  readonly method?: CodeableConcept;
  /** Amount per dose and, for infusions, per unit of time. */
  readonly doseAndRate?: readonly DosageDoseAndRate[];
}

/**
 * An order or prescription for a medication.
 *
 * `medication` is a FHIR choice element: a server sends either
 * `medicationCodeableConcept` (usually an RxNorm code) or `medicationReference` to
 * a Medication resource. Exactly one is present, and FHIR requires that it be one
 * of them, but which one varies by EHR — so both are optional here and the caller
 * checks whichever arrived.
 */
export interface MedicationRequest extends DomainResource {
  readonly resourceType: 'MedicationRequest';
  /** Business identifiers for the request. */
  readonly identifier?: readonly Identifier[];
  /** Lifecycle state of the request. Required by FHIR. */
  readonly status: MedicationRequestStatus;
  /** Why the request is in its current state. */
  readonly statusReason?: CodeableConcept;
  /** Whether this is a proposal, plan or actual order. Required by FHIR. */
  readonly intent: MedicationRequestIntent;
  /** Where the medication is expected to be administered. */
  readonly category?: readonly CodeableConcept[];
  /** Urgency of the request. */
  readonly priority?: RequestPriority;
  /** The drug, coded — typically RxNorm. */
  readonly medicationCodeableConcept?: CodeableConcept;
  /** The drug, as a reference to a Medication resource. */
  readonly medicationReference?: Reference;
  /** The patient the medication is for. Required by FHIR. */
  readonly subject: Reference;
  /** Encounter the request was made during. */
  readonly encounter?: Reference;
  /** Other resources informing the request. */
  readonly supportingInformation?: readonly Reference[];
  /** When the request was written, as a `dateTime`. */
  readonly authoredOn?: string;
  /** Who ordered the medication. */
  readonly requester?: Reference;
  /** Who entered the order on the requester's behalf. */
  readonly recorder?: Reference;
  /** Coded reason for ordering — the clinical justification a payer evaluates the
   * request against. */
  readonly reasonCode?: readonly CodeableConcept[];
  /** Reason expressed as a Condition or Observation reference. */
  readonly reasonReference?: readonly Reference[];
  /** Coverage expected to pay for the medication. */
  readonly insurance?: readonly Reference[];
  /** Free-text notes about the request. PHI. */
  readonly note?: readonly Annotation[];
  /** How the medication is to be taken. */
  readonly dosageInstruction?: readonly Dosage[];
  /** The order this one replaces. */
  readonly priorPrescription?: Reference;
}

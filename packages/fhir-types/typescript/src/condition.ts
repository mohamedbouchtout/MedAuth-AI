/**
 * The FHIR R4 Condition resource, mirroring `src/fhir_types/condition.py`.
 *
 * Conditions supply the diagnosis codes a payer policy is evaluated against — the
 * ICD-10 side of the "does this order meet prior authorization criteria" question.
 */

import type { DomainResource } from './base.js';
import type { Annotation, CodeableConcept, Identifier, Reference } from './datatypes.js';

/**
 * A clinical condition, problem or diagnosis.
 *
 * `clinicalStatus` and `verificationStatus` are `CodeableConcept` rather than
 * string unions, matching R4: both have required bindings, but the codes are
 * carried inside a coding with a fixed system URI rather than as a bare code.
 *
 * `onset` and `abatement` are FHIR choice elements with five variants each; only
 * the `dateTime` and `string` forms are modelled, because those are what the EHRs
 * on our integration list actually send.
 */
export interface Condition extends DomainResource {
  readonly resourceType: 'Condition';
  /** Business identifiers for the condition. */
  readonly identifier?: readonly Identifier[];
  /** active, recurrence, relapse, inactive, remission, resolved. */
  readonly clinicalStatus?: CodeableConcept;
  /** unconfirmed through confirmed, refuted, entered-in-error. */
  readonly verificationStatus?: CodeableConcept;
  /** problem-list-item or encounter-diagnosis. */
  readonly category?: readonly CodeableConcept[];
  /** Subjective severity of the condition. */
  readonly severity?: CodeableConcept;
  /** The condition itself, coded — ICD-10 and/or SNOMED. */
  readonly code?: CodeableConcept;
  /** Anatomical location, which several orthopedic policies key on. */
  readonly bodySite?: readonly CodeableConcept[];
  /** The patient with the condition. Required by FHIR. */
  readonly subject: Reference;
  /** Encounter during which the condition was first asserted. */
  readonly encounter?: Reference;
  /** When the condition began, as a `dateTime`. */
  readonly onsetDateTime?: string;
  /** When the condition began, as free text. */
  readonly onsetString?: string;
  /** When the condition resolved, as a `dateTime`. */
  readonly abatementDateTime?: string;
  /** When the condition resolved, as free text. */
  readonly abatementString?: string;
  /** When the record was first captured. */
  readonly recordedDate?: string;
  /** Who recorded the condition. */
  readonly recorder?: Reference;
  /** Who asserted the condition is present. */
  readonly asserter?: Reference;
  /** Free-text notes about the condition. PHI. */
  readonly note?: readonly Annotation[];
}

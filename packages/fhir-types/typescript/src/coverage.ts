/**
 * The FHIR R4 Coverage resource, mirroring `src/fhir_types/coverage.py`.
 *
 * Coverage is what makes the RAG query answerable: the payer, plan type and member
 * details here select which insurance policy set applies. Together with the
 * patient's state and the ordered CPT code they form the
 * `rag:{payer}:{plan_type}:{state}:{cpt_code}` cache key described in CLAUDE.md.
 */

import type { DomainResource } from './base.js';
import type { FinancialResourceStatus } from './codes.js';
import type { CodeableConcept, Identifier, Period, Reference } from './datatypes.js';

/**
 * One classification of the coverage — group, plan, subplan and similar.
 *
 * The plan type that selects a payer policy usually arrives here, as the entry
 * whose `type` coding is `plan`.
 */
export interface CoverageClass {
  /** Which kind of classification this entry is. Required by FHIR. */
  readonly type: CodeableConcept;
  /** The identifier or code for that classification. Required by FHIR. */
  readonly value: string;
  /** Human-readable name for it. */
  readonly name?: string;
}

/**
 * Insurance or medical plan coverage for a patient.
 *
 * The `class` property keeps its FHIR name here. On the Python side it is
 * `coverage_class`, because `class` is a reserved word there.
 */
export interface Coverage extends DomainResource {
  readonly resourceType: 'Coverage';
  /** The member id and other business identifiers. */
  readonly identifier?: readonly Identifier[];
  /** Lifecycle state of the coverage record. Required by FHIR. */
  readonly status: FinancialResourceStatus;
  /** Type of coverage — the plan category, e.g. PPO or HMO. */
  readonly type?: CodeableConcept;
  /** Owner of the policy. */
  readonly policyHolder?: Reference;
  /** The person the policy is issued to. */
  readonly subscriber?: Reference;
  /** The subscriber's id with the payer. PHI. */
  readonly subscriberId?: string;
  /** The covered patient. Required by FHIR. */
  readonly beneficiary: Reference;
  /** Dependent number under the policy. */
  readonly dependent?: string;
  /** The beneficiary's relationship to the subscriber. */
  readonly relationship?: CodeableConcept;
  /** When the coverage is in force. */
  readonly period?: Period;
  /** The insurers responsible for payment. Required by FHIR — this is the payer
   * whose policies the RAG query is scoped to. */
  readonly payor: readonly Reference[];
  /** Group, plan and subplan classifications. */
  readonly class?: readonly CoverageClass[];
  /** Relative order of this coverage when several apply. */
  readonly order?: number;
  /** The insurer's network the patient is in, which changes criteria on many
   * policies. */
  readonly network?: string;
}

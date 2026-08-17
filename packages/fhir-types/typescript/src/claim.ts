/**
 * The FHIR R4 Claim resource, mirroring `src/fhir_types/claim.py`.
 *
 * Prior authorization requests are Claims with `use = 'preauthorization'`,
 * submitted through Da Vinci PAS. Athenahealth does not support FHIR PAS and its
 * adapter routes through CoverMyMeds instead (see CLAUDE.md) — the bundle is still
 * built as a Claim first, then translated, so this shape stays the single internal
 * representation.
 */

import type { DomainResource } from './base.js';
import type { ClaimUse, FinancialResourceStatus } from './codes.js';
import type {
  CodeableConcept,
  Identifier,
  Money,
  Period,
  Quantity,
  Reference,
} from './datatypes.js';

/** A provider involved in the requested care. */
export interface ClaimCareTeam {
  /** Position in the care team list, referenced by items. Required. */
  readonly sequence: number;
  /** The practitioner or organization. Required by FHIR. */
  readonly provider: Reference;
  /** Whether this provider is the responsible party. */
  readonly responsible?: boolean;
  /** The role played — ordering, rendering, supervising. */
  readonly role?: CodeableConcept;
  /** The provider's qualification for the service. */
  readonly qualification?: CodeableConcept;
}

/**
 * Additional evidence supporting the request.
 *
 * This is where the clinical justification for a prior authorization goes —
 * conservative-treatment history, imaging findings, prior therapy durations. Payer
 * criteria are evaluated against these entries, so a missing one is the usual
 * cause of a denial.
 */
export interface ClaimSupportingInfo {
  /** Position in the supporting-info list, referenced by items. */
  readonly sequence: number;
  /** Classification of the supplied information. Required by FHIR. */
  readonly category: CodeableConcept;
  /** Coded detail of the information. */
  readonly code?: CodeableConcept;
  /** When the supporting event occurred, as a `date`. */
  readonly timingDate?: string;
  /** Period the supporting information covers. */
  readonly timingPeriod?: Period;
  /** The information as free text. Frequently PHI. */
  readonly valueString?: string;
  /** The information as a measured amount. */
  readonly valueQuantity?: Quantity;
  /** The information as a reference to another resource. */
  readonly valueReference?: Reference;
  /** Why the information is being supplied. */
  readonly reason?: CodeableConcept;
}

/** A diagnosis relevant to the claim. */
export interface ClaimDiagnosis {
  /** Position in the diagnosis list, referenced by items. Required. */
  readonly sequence: number;
  /** The diagnosis, coded — usually ICD-10-CM. */
  readonly diagnosisCodeableConcept?: CodeableConcept;
  /** The diagnosis, as a reference to a Condition. */
  readonly diagnosisReference?: Reference;
  /** Role of the diagnosis, e.g. principal. */
  readonly type?: readonly CodeableConcept[];
  /** Whether the diagnosis was present on admission. */
  readonly onAdmission?: CodeableConcept;
  /** DRG or similar grouping code. */
  readonly packageCode?: CodeableConcept;
}

/** A procedure performed or proposed. */
export interface ClaimProcedure {
  /** Position in the procedure list, referenced by items. Required. */
  readonly sequence: number;
  /** Role of the procedure, e.g. primary. */
  readonly type?: readonly CodeableConcept[];
  /** When the procedure was performed, as a `dateTime`. */
  readonly date?: string;
  /** The procedure, coded — usually CPT or HCPCS. */
  readonly procedureCodeableConcept?: CodeableConcept;
  /** The procedure, as a reference to a Procedure resource. */
  readonly procedureReference?: Reference;
}

/** One coverage the claim is being submitted against. */
export interface ClaimInsurance {
  /** Order in which coverages are applied. Required by FHIR. */
  readonly sequence: number;
  /** Whether this coverage is the one being adjudicated. Required. */
  readonly focal: boolean;
  /** The claim's identifier as issued by this insurer. */
  readonly identifier?: Identifier;
  /** Reference to the Coverage resource. Required by FHIR. */
  readonly coverage: Reference;
  /** Contract number under which the claim is made. */
  readonly businessArrangement?: string;
  /** Authorization numbers already issued by the payer — the field that carries
   * the approval back into a subsequent claim. */
  readonly preAuthRef?: readonly string[];
  /** The adjudication response, once one exists. */
  readonly claimResponse?: Reference;
}

/**
 * A product or service being claimed or requested.
 *
 * The CPT code in `productOrService` is the one the RAG query is keyed on; the
 * `diagnosisSequence` and `informationSequence` pointers are what tie an item to
 * the diagnoses and evidence that justify it.
 */
export interface ClaimItem {
  /** Position in the item list. Required by FHIR. */
  readonly sequence: number;
  /** Indices into the claim's care team. */
  readonly careTeamSequence?: readonly number[];
  /** Indices into the claim's diagnosis list. */
  readonly diagnosisSequence?: readonly number[];
  /** Indices into the claim's supporting-info list. */
  readonly informationSequence?: readonly number[];
  /** Indices into the claim's procedure list. */
  readonly procedureSequence?: readonly number[];
  /** Revenue or cost center code. */
  readonly revenue?: CodeableConcept;
  /** Benefit classification of the service. */
  readonly category?: CodeableConcept;
  /** The service or product itself — the CPT or HCPCS code. Required by FHIR. */
  readonly productOrService: CodeableConcept;
  /** Modifiers qualifying the code, which frequently change whether a payer
   * requires authorization at all. */
  readonly modifier?: readonly CodeableConcept[];
  /** Program the item is claimed under. */
  readonly programCode?: readonly CodeableConcept[];
  /** Date of service, as a `date`. */
  readonly servicedDate?: string;
  /** Period of service, when it spans days. */
  readonly servicedPeriod?: Period;
  /** Number of units requested. */
  readonly quantity?: Quantity;
  /** Fee per unit. */
  readonly unitPrice?: Money;
  /** Multiplier applied to the price. */
  readonly factor?: number;
  /** Total charge for the item. */
  readonly net?: Money;
  /** Anatomical site the service applies to. */
  readonly bodySite?: CodeableConcept;
  /** More specific sub-location within the body site. */
  readonly subSite?: readonly CodeableConcept[];
  /** Encounters related to this item. */
  readonly encounter?: readonly Reference[];
}

/**
 * A request to an insurer for adjudication, reimbursement or authorization.
 *
 * Set `use = 'preauthorization'` for a prior authorization request;
 * `'predetermination'` asks the payer what it would authorize without committing
 * to the service, and `'claim'` is post-service billing.
 */
export interface Claim extends DomainResource {
  readonly resourceType: 'Claim';
  /** Business identifiers for the claim. */
  readonly identifier?: readonly Identifier[];
  /** Lifecycle state of the claim record. Required by FHIR. */
  readonly status: FinancialResourceStatus;
  /** Category of claim — professional, institutional, pharmacy. Required. */
  readonly type: CodeableConcept;
  /** Finer categorization within the type. */
  readonly subType?: CodeableConcept;
  /** Whether this is a claim, preauthorization or predetermination. Required. */
  readonly use: ClaimUse;
  /** The patient the request is for. Required by FHIR. */
  readonly patient: Reference;
  /** Period the claim covers. */
  readonly billablePeriod?: Period;
  /** When the claim was created, as a `dateTime`. Required by FHIR. */
  readonly created: string;
  /** Who entered the claim. */
  readonly enterer?: Reference;
  /** The target payer. */
  readonly insurer?: Reference;
  /** The submitting provider or organization. Required by FHIR. */
  readonly provider: Reference;
  /** Urgency of processing. Required by FHIR. */
  readonly priority: CodeableConcept;
  /** Referral authorizing the requested service. */
  readonly referral?: Reference;
  /** Where the service will be or was provided. */
  readonly facility?: Reference;
  /** Providers involved in the requested care. */
  readonly careTeam?: readonly ClaimCareTeam[];
  /** Evidence supporting the request. */
  readonly supportingInfo?: readonly ClaimSupportingInfo[];
  /** Diagnoses justifying the requested services. */
  readonly diagnosis?: readonly ClaimDiagnosis[];
  /** Procedures relevant to the request. */
  readonly procedure?: readonly ClaimProcedure[];
  /** Coverages the request is submitted against. Required by FHIR. */
  readonly insurance: readonly ClaimInsurance[];
  /** The products and services being requested. */
  readonly item?: readonly ClaimItem[];
  /** Total value of all items. */
  readonly total?: Money;
}

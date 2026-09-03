/**
 * The FHIR R4 ClaimResponse resource, mirroring `src/fhir_types/claim_response.py`.
 *
 * This is what a payer sends back for a prior authorization: Da Vinci PAS's
 * `Claim/$submit` answers with a bundle whose first entry is a ClaimResponse. The
 * authorization number a later claim must quote is `preAuthRef`; whether the
 * request was approved is in `item.adjudication`, never in `outcome` — see
 * `RemittanceOutcome` in `codes.ts`.
 *
 * The elements mirrored are the ones a prior-authorization response is read for.
 * Post-payment elements — `payment`, `addItem`, `fundsReserve`, `form`, and the
 * `detail`/`subDetail` levels under an item — belong to billing rather than to
 * authorization and are not modelled on either side.
 *
 * @remarks
 * Formatting here is load-bearing — see `base.ts`.
 */

import type { DomainResource } from './base.js';
import type {
  ClaimUse,
  FinancialResourceStatus,
  NoteType,
  RemittanceOutcome,
} from './codes.js';
import type { CodeableConcept, Identifier, Money, Period, Reference } from './datatypes.js';

/**
 * One payer decision about an item, and the reason for it.
 *
 * This is where a prior authorization is actually approved or denied.
 */
export interface ClaimResponseAdjudication {
  /** Which kind of decision this entry reports. Required by FHIR. */
  readonly category: CodeableConcept;
  /** The payer's stated reason for the decision. */
  readonly reason?: CodeableConcept;
  /** Monetary result of the decision. */
  readonly amount?: Money;
  /** Non-monetary result, e.g. a percentage. */
  readonly value?: number;
}

/** The payer's response to one requested product or service. */
export interface ClaimResponseItem {
  /**
   * The `Claim.item.sequence` this responds to. Required by FHIR, and the only
   * link back to what was asked for — an item read without it cannot be matched
   * to a requested procedure.
   */
  readonly itemSequence: number;
  /** Indices into `processNote` that apply to this item. */
  readonly noteNumber?: readonly number[];
  /** The decisions made about this item. Required by FHIR. */
  readonly adjudication: readonly ClaimResponseAdjudication[];
}

/** A monetary total across the whole response. */
export interface ClaimResponseTotal {
  /** Which total this is — submitted, benefit, and so on. Required. */
  readonly category: CodeableConcept;
  /** The amount itself. Required by FHIR. */
  readonly amount: Money;
}

/** One coverage considered when adjudicating. */
export interface ClaimResponseInsurance {
  /** Order in which coverages are applied. Required by FHIR. */
  readonly sequence: number;
  /** Whether this is the coverage the response adjudicates. Required. */
  readonly focal: boolean;
  /** Reference to the Coverage resource. Required by FHIR. */
  readonly coverage: Reference;
  /** Contract number under which the claim was made. */
  readonly businessArrangement?: string;
  /** The adjudication from another insurer, when coordinating. */
  readonly claimResponse?: Reference;
}

/**
 * A processing error the payer reports against the submission.
 *
 * An error entry is why a request was not adjudicated. The three sequence
 * elements locate it in the submitted Claim; all are absent when the error is
 * about the request as a whole.
 */
export interface ClaimResponseError {
  /** The `Claim.item.sequence` in error. */
  readonly itemSequence?: number;
  /** The detail within that item, when the error is narrower. */
  readonly detailSequence?: number;
  /** The sub-detail within that detail. */
  readonly subDetailSequence?: number;
  /** The error itself, coded. Required by FHIR. */
  readonly code: CodeableConcept;
}

/**
 * A human-readable note about the adjudication.
 *
 * Payers put the substance of a pended or denied authorization here — what
 * documentation is still wanted, which criterion was not met — so it is the text
 * a provider actually needs to read.
 */
export interface ClaimResponseProcessNote {
  /** This note's number, referenced by `item.noteNumber`. */
  readonly number?: number;
  /** Whether the note is for display, for print, or for the operator. */
  readonly type?: NoteType;
  /** The note itself. Required by FHIR. */
  readonly text: string;
  /** Language of the note, as a CodeableConcept. */
  readonly language?: CodeableConcept;
}

/** A payer's adjudication of a claim, predetermination or preauthorization. */
export interface ClaimResponse extends DomainResource {
  readonly resourceType: 'ClaimResponse';
  /** Business identifiers for the response. */
  readonly identifier?: readonly Identifier[];
  /** Lifecycle state of the response record. Required by FHIR. */
  readonly status: FinancialResourceStatus;
  /** Category of claim being responded to. Required by FHIR. */
  readonly type: CodeableConcept;
  /** Finer categorization within the type. */
  readonly subType?: CodeableConcept;
  /** Whether the request was a claim, preauthorization or predetermination. */
  readonly use: ClaimUse;
  /** The patient the request was for. Required by FHIR. */
  readonly patient: Reference;
  /** When the response was created, as a `dateTime`. Required. */
  readonly created: string;
  /** The payer that adjudicated. Required by FHIR. */
  readonly insurer: Reference;
  /** Who submitted the request. */
  readonly requestor?: Reference;
  /** Reference to the Claim being responded to. */
  readonly request?: Reference;
  /** Whether the request was *processed*, not whether it was approved. Required. */
  readonly outcome: RemittanceOutcome;
  /** Human-readable summary of the outcome. */
  readonly disposition?: string;
  /**
   * The authorization number, when one was issued. This is what a subsequent
   * claim quotes in `Claim.insurance.preAuthRef`.
   */
  readonly preAuthRef?: string;
  /**
   * How long the authorization is valid for. An approval read without it looks
   * open-ended when it is not.
   */
  readonly preAuthPeriod?: Period;
  /** Who is to be paid, when the response concerns payment. */
  readonly payeeType?: CodeableConcept;
  /** The decisions made about each requested product or service. */
  readonly item?: readonly ClaimResponseItem[];
  /** Decisions that apply to the request as a whole rather than to one item. */
  readonly adjudication?: readonly ClaimResponseAdjudication[];
  /** Monetary totals across the response. */
  readonly total?: readonly ClaimResponseTotal[];
  /** Coverages considered when adjudicating. */
  readonly insurance?: readonly ClaimResponseInsurance[];
  /** Processing errors — why a request was not adjudicated. */
  readonly error?: readonly ClaimResponseError[];
  /** Human-readable notes about the adjudication. */
  readonly processNote?: readonly ClaimResponseProcessNote[];
  /** Requests for further information from the payer. */
  readonly communicationRequest?: readonly Reference[];
  /** Code for the printed form the response should use. */
  readonly formCode?: CodeableConcept;
}

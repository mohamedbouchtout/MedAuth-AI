"""The FHIR R4 ClaimResponse resource.

This is what a payer sends back for a prior authorization: Da Vinci PAS's
``Claim/$submit`` answers with a bundle whose first entry is a ClaimResponse. The
authorization number a later claim must quote is ``pre_auth_ref``; whether the
request was approved is in ``item.adjudication``, never in ``outcome`` — see
``RemittanceOutcome`` in ``codes.py``.

The elements modelled are the ones a prior-authorization response is read for:
``item`` and its ``adjudication``, plus ``total``, ``insurance``, ``error`` and
``process_note``. Post-payment elements — ``payment``, ``add_item``,
``funds_reserve``, ``form``, and the ``detail``/``sub_detail`` levels under an item
— belong to billing rather than to authorization and are not modelled; a payer that
sends one still round-trips it through ``extra="allow"``.
"""

from __future__ import annotations

from typing import Literal

from .base import DomainResource, FHIRBase
from .codes import ClaimUse, FinancialResourceStatus, NoteType, RemittanceOutcome
from .datatypes import CodeableConcept, Identifier, Money, Period, Reference


class ClaimResponseAdjudication(FHIRBase):
    """One payer decision about an item, and the reason for it.

    This is where a prior authorization is actually approved or denied. The
    category says which decision is being reported, and ``reason`` carries the
    payer's stated grounds when there are any.

    Attributes:
        category: Which kind of decision this entry reports. Required by FHIR.
        reason: The payer's stated reason for the decision.
        amount: Monetary result of the decision.
        value: Non-monetary result, e.g. a percentage.
    """

    category: CodeableConcept
    reason: CodeableConcept | None = None
    amount: Money | None = None
    value: float | None = None


class ClaimResponseItem(FHIRBase):
    """The payer's response to one requested product or service.

    Attributes:
        item_sequence: The ``Claim.item.sequence`` this responds to. Required by
            FHIR, and the only link back to what was asked for — an item read
            without it cannot be matched to a requested procedure.
        note_number: Indices into ``process_note`` that apply to this item.
        adjudication: The decisions made about this item. Required by FHIR.
    """

    item_sequence: int
    note_number: list[int] | None = None
    adjudication: list[ClaimResponseAdjudication]


class ClaimResponseTotal(FHIRBase):
    """A monetary total across the whole response.

    Attributes:
        category: Which total this is — submitted, benefit, and so on. Required.
        amount: The amount itself. Required by FHIR.
    """

    category: CodeableConcept
    amount: Money


class ClaimResponseInsurance(FHIRBase):
    """One coverage considered when adjudicating.

    Attributes:
        sequence: Order in which coverages are applied. Required by FHIR.
        focal: Whether this is the coverage the response adjudicates. Required.
        coverage: Reference to the Coverage resource. Required by FHIR.
        business_arrangement: Contract number under which the claim was made.
        claim_response: The adjudication from another insurer, when coordinating.
    """

    sequence: int
    focal: bool
    coverage: Reference
    business_arrangement: str | None = None
    claim_response: Reference | None = None


class ClaimResponseError(FHIRBase):
    """A processing error the payer reports against the submission.

    An error entry is why a request was not adjudicated — a missing element, an
    unrecognised code. The three sequence elements locate it in the submitted
    Claim; all are absent when the error is about the request as a whole.

    Attributes:
        item_sequence: The ``Claim.item.sequence`` in error.
        detail_sequence: The detail within that item, when the error is narrower.
        sub_detail_sequence: The sub-detail within that detail.
        code: The error itself, coded. Required by FHIR.
    """

    item_sequence: int | None = None
    detail_sequence: int | None = None
    sub_detail_sequence: int | None = None
    code: CodeableConcept


class ClaimResponseProcessNote(FHIRBase):
    """A human-readable note about the adjudication.

    Payers put the substance of a pended or denied authorization here — what
    documentation is still wanted, which criterion was not met — so it is the text
    a provider actually needs to read.

    Attributes:
        number: This note's number, referenced by ``item.note_number``.
        type: Whether the note is for display, for print, or for the operator.
        text: The note itself. Required by FHIR.
        language: Language of the note, as a CodeableConcept.
    """

    number: int | None = None
    type: NoteType | None = None
    text: str
    language: CodeableConcept | None = None


class ClaimResponse(DomainResource):
    """A payer's adjudication of a claim, predetermination or preauthorization.

    Attributes:
        identifier: Business identifiers for the response.
        status: Lifecycle state of the response record. Required by FHIR.
        type: Category of claim being responded to. Required by FHIR.
        sub_type: Finer categorization within the type.
        use: Whether the request was a claim, preauthorization or
            predetermination. Required by FHIR.
        patient: The patient the request was for. Required by FHIR.
        created: When the response was created, as a ``dateTime``. Required.
        insurer: The payer that adjudicated. Required by FHIR.
        requestor: Who submitted the request.
        request: Reference to the Claim being responded to.
        outcome: Whether the request was *processed*, not whether it was approved.
            Required by FHIR.
        disposition: Human-readable summary of the outcome.
        pre_auth_ref: The authorization number, when one was issued. This is what
            a subsequent claim quotes in ``Claim.insurance.preAuthRef``.
        pre_auth_period: How long the authorization is valid for. An approval read
            without it looks open-ended when it is not.
        payee_type: Who is to be paid, when the response concerns payment.
        item: The decisions made about each requested product or service.
        adjudication: Decisions that apply to the request as a whole rather than
            to one item.
        total: Monetary totals across the response.
        insurance: Coverages considered when adjudicating.
        error: Processing errors — why a request was not adjudicated.
        process_note: Human-readable notes about the adjudication.
        communication_request: Requests for further information from the payer.
        form_code: Code for the printed form the response should use.
    """

    resource_type: Literal["ClaimResponse"] = "ClaimResponse"
    identifier: list[Identifier] | None = None
    status: FinancialResourceStatus
    type: CodeableConcept
    sub_type: CodeableConcept | None = None
    use: ClaimUse
    patient: Reference
    created: str
    insurer: Reference
    requestor: Reference | None = None
    request: Reference | None = None
    outcome: RemittanceOutcome
    disposition: str | None = None
    pre_auth_ref: str | None = None
    pre_auth_period: Period | None = None
    payee_type: CodeableConcept | None = None
    item: list[ClaimResponseItem] | None = None
    adjudication: list[ClaimResponseAdjudication] | None = None
    total: list[ClaimResponseTotal] | None = None
    insurance: list[ClaimResponseInsurance] | None = None
    error: list[ClaimResponseError] | None = None
    process_note: list[ClaimResponseProcessNote] | None = None
    communication_request: list[Reference] | None = None
    form_code: CodeableConcept | None = None

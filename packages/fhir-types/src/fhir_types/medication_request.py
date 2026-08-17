"""The FHIR R4 MedicationRequest resource.

Medication orders are a prior authorization trigger in their own right — biologics,
specialty infusions and chemotherapy are authorized off the drug, not a procedure
code — so this resource sits alongside Claim in the prior-auth bundle rather than
being purely informational.
"""

from __future__ import annotations

from typing import Literal

from .base import DomainResource, FHIRBase
from .codes import MedicationRequestIntent, MedicationRequestStatus, RequestPriority
from .datatypes import Annotation, CodeableConcept, Identifier, Quantity, Reference


class DosageDoseAndRate(FHIRBase):
    """The amount of medication administered per dose.

    Attributes:
        type: Whether this is the ordered, calculated or adjusted amount.
        dose_quantity: Amount per administration.
        rate_quantity: Amount per unit of time, for infusions.
    """

    type: CodeableConcept | None = None
    dose_quantity: Quantity | None = None
    rate_quantity: Quantity | None = None


class Dosage(FHIRBase):
    """How the medication is to be taken.

    ``timing`` is not modelled: FHIR's Timing datatype is large, and no code in this
    project reads it. ``extra="allow"`` keeps it intact through a round trip, so
    modelling it later is additive rather than a breaking change.

    Attributes:
        sequence: Order in which to apply multiple dosage instructions.
        text: Free-text dosing instructions. PHI.
        additional_instruction: Supplemental coded instructions.
        patient_instruction: Instructions in terms the patient can act on. PHI.
        as_needed_boolean: Whether the medication is taken only as needed.
        site: Body site of administration.
        route: How the medication enters the body.
        method: Technique of administration.
        dose_and_rate: Amount per dose and, for infusions, per unit of time.
    """

    sequence: int | None = None
    text: str | None = None
    additional_instruction: list[CodeableConcept] | None = None
    patient_instruction: str | None = None
    as_needed_boolean: bool | None = None
    site: CodeableConcept | None = None
    route: CodeableConcept | None = None
    method: CodeableConcept | None = None
    dose_and_rate: list[DosageDoseAndRate] | None = None


class MedicationRequest(DomainResource):
    """An order or prescription for a medication.

    ``medication`` is a FHIR choice element: a server sends either
    ``medicationCodeableConcept`` (usually an RxNorm code) or
    ``medicationReference`` to a Medication resource. Exactly one is present, and
    FHIR requires that it be one of them, but which one varies by EHR — so both are
    modelled as optional and the caller checks whichever arrived.

    Attributes:
        identifier: Business identifiers for the request.
        status: Lifecycle state of the request. Required by FHIR.
        status_reason: Why the request is in its current state.
        intent: Whether this is a proposal, plan or actual order. Required by FHIR.
        category: Where the medication is expected to be administered.
        priority: Urgency of the request.
        medication_codeable_concept: The drug, coded — typically RxNorm.
        medication_reference: The drug, as a reference to a Medication resource.
        subject: The patient the medication is for. Required by FHIR.
        encounter: Encounter the request was made during.
        supporting_information: Other resources informing the request.
        authored_on: When the request was written, as a ``dateTime``.
        requester: Who ordered the medication.
        recorder: Who entered the order on the requester's behalf.
        reason_code: Coded reason for ordering — the clinical justification a payer
            evaluates the request against.
        reason_reference: Reason expressed as a Condition or Observation reference.
        insurance: Coverage expected to pay for the medication.
        note: Free-text notes about the request. PHI.
        dosage_instruction: How the medication is to be taken.
        prior_prescription: The order this one replaces.
    """

    resource_type: Literal["MedicationRequest"] = "MedicationRequest"
    identifier: list[Identifier] | None = None
    status: MedicationRequestStatus
    status_reason: CodeableConcept | None = None
    intent: MedicationRequestIntent
    category: list[CodeableConcept] | None = None
    priority: RequestPriority | None = None
    medication_codeable_concept: CodeableConcept | None = None
    medication_reference: Reference | None = None
    subject: Reference
    encounter: Reference | None = None
    supporting_information: list[Reference] | None = None
    authored_on: str | None = None
    requester: Reference | None = None
    recorder: Reference | None = None
    reason_code: list[CodeableConcept] | None = None
    reason_reference: list[Reference] | None = None
    insurance: list[Reference] | None = None
    note: list[Annotation] | None = None
    dosage_instruction: list[Dosage] | None = None
    prior_prescription: Reference | None = None

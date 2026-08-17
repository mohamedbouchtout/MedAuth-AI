"""The FHIR R4 Claim resource.

Prior authorization requests are Claims with ``use = "preauthorization"``, submitted
through Da Vinci PAS. This is the resource ``submit_prior_auth()`` assembles on the
adapter base class. Athenahealth does not support FHIR PAS and its adapter routes
through CoverMyMeds instead (see CLAUDE.md) — the bundle is still built as a Claim
first, then translated, so this shape stays the single internal representation.
"""

from __future__ import annotations

from typing import Literal

from .base import DomainResource, FHIRBase
from .codes import ClaimUse, FinancialResourceStatus
from .datatypes import CodeableConcept, Identifier, Money, Period, Quantity, Reference


class ClaimCareTeam(FHIRBase):
    """A provider involved in the requested care.

    Attributes:
        sequence: Position in the care team list, referenced by items. Required.
        provider: The practitioner or organization. Required by FHIR.
        responsible: Whether this provider is the responsible party.
        role: The role played — ordering, rendering, supervising.
        qualification: The provider's qualification for the service.
    """

    sequence: int
    provider: Reference
    responsible: bool | None = None
    role: CodeableConcept | None = None
    qualification: CodeableConcept | None = None


class ClaimSupportingInfo(FHIRBase):
    """Additional evidence supporting the request.

    This is where the clinical justification for a prior authorization goes —
    conservative-treatment history, imaging findings, prior therapy durations.
    Payer criteria are evaluated against these entries, so a missing one is the
    usual cause of a denial.

    Attributes:
        sequence: Position in the supporting-info list, referenced by items.
        category: Classification of the supplied information. Required by FHIR.
        code: Coded detail of the information.
        timing_date: When the supporting event occurred, as a ``date``.
        timing_period: Period the supporting information covers.
        value_string: The information as free text. Frequently PHI.
        value_quantity: The information as a measured amount.
        value_reference: The information as a reference to another resource.
        reason: Why the information is being supplied.
    """

    sequence: int
    category: CodeableConcept
    code: CodeableConcept | None = None
    timing_date: str | None = None
    timing_period: Period | None = None
    value_string: str | None = None
    value_quantity: Quantity | None = None
    value_reference: Reference | None = None
    reason: CodeableConcept | None = None


class ClaimDiagnosis(FHIRBase):
    """A diagnosis relevant to the claim.

    Attributes:
        sequence: Position in the diagnosis list, referenced by items. Required.
        diagnosis_codeable_concept: The diagnosis, coded — usually ICD-10-CM.
        diagnosis_reference: The diagnosis, as a reference to a Condition.
        type: Role of the diagnosis, e.g. principal.
        on_admission: Whether the diagnosis was present on admission.
        package_code: DRG or similar grouping code.
    """

    sequence: int
    diagnosis_codeable_concept: CodeableConcept | None = None
    diagnosis_reference: Reference | None = None
    type: list[CodeableConcept] | None = None
    on_admission: CodeableConcept | None = None
    package_code: CodeableConcept | None = None


class ClaimProcedure(FHIRBase):
    """A procedure performed or proposed.

    Attributes:
        sequence: Position in the procedure list, referenced by items. Required.
        type: Role of the procedure, e.g. primary.
        date: When the procedure was performed, as a ``dateTime``.
        procedure_codeable_concept: The procedure, coded — usually CPT or HCPCS.
        procedure_reference: The procedure, as a reference to a Procedure resource.
    """

    sequence: int
    type: list[CodeableConcept] | None = None
    date: str | None = None
    procedure_codeable_concept: CodeableConcept | None = None
    procedure_reference: Reference | None = None


class ClaimInsurance(FHIRBase):
    """One coverage the claim is being submitted against.

    Attributes:
        sequence: Order in which coverages are applied. Required by FHIR.
        focal: Whether this coverage is the one being adjudicated. Required.
        identifier: The claim's identifier as issued by this insurer.
        coverage: Reference to the Coverage resource. Required by FHIR.
        business_arrangement: Contract number under which the claim is made.
        pre_auth_ref: Authorization numbers already issued by the payer — the field
            that carries the approval back into a subsequent claim.
        claim_response: The adjudication response, once one exists.
    """

    sequence: int
    focal: bool
    identifier: Identifier | None = None
    coverage: Reference
    business_arrangement: str | None = None
    pre_auth_ref: list[str] | None = None
    claim_response: Reference | None = None


class ClaimItem(FHIRBase):
    """A product or service being claimed or requested.

    The CPT code in ``product_or_service`` is the one the RAG query is keyed on;
    the ``diagnosis_sequence`` and ``information_sequence`` pointers are what tie an
    item to the diagnoses and evidence that justify it.

    Attributes:
        sequence: Position in the item list. Required by FHIR.
        care_team_sequence: Indices into the claim's care team.
        diagnosis_sequence: Indices into the claim's diagnosis list.
        information_sequence: Indices into the claim's supporting-info list.
        procedure_sequence: Indices into the claim's procedure list.
        revenue: Revenue or cost center code.
        category: Benefit classification of the service.
        product_or_service: The service or product itself — the CPT or HCPCS code.
            Required by FHIR.
        modifier: Modifiers qualifying the code, which frequently change whether a
            payer requires authorization at all.
        program_code: Program the item is claimed under.
        serviced_date: Date of service, as a ``date``.
        serviced_period: Period of service, when it spans days.
        quantity: Number of units requested.
        unit_price: Fee per unit.
        factor: Multiplier applied to the price.
        net: Total charge for the item.
        body_site: Anatomical site the service applies to.
        sub_site: More specific sub-location within the body site.
        encounter: Encounters related to this item.
    """

    sequence: int
    care_team_sequence: list[int] | None = None
    diagnosis_sequence: list[int] | None = None
    information_sequence: list[int] | None = None
    procedure_sequence: list[int] | None = None
    revenue: CodeableConcept | None = None
    category: CodeableConcept | None = None
    product_or_service: CodeableConcept
    modifier: list[CodeableConcept] | None = None
    program_code: list[CodeableConcept] | None = None
    serviced_date: str | None = None
    serviced_period: Period | None = None
    quantity: Quantity | None = None
    unit_price: Money | None = None
    factor: float | None = None
    net: Money | None = None
    body_site: CodeableConcept | None = None
    sub_site: list[CodeableConcept] | None = None
    encounter: list[Reference] | None = None


class Claim(DomainResource):
    """A request to an insurer for adjudication, reimbursement or authorization.

    Set ``use = "preauthorization"`` for a prior authorization request;
    ``"predetermination"`` asks the payer what it would authorize without committing
    to the service, and ``"claim"`` is post-service billing.

    Attributes:
        identifier: Business identifiers for the claim.
        status: Lifecycle state of the claim record. Required by FHIR.
        type: Category of claim — professional, institutional, pharmacy. Required.
        sub_type: Finer categorization within the type.
        use: Whether this is a claim, preauthorization or predetermination.
            Required by FHIR.
        patient: The patient the request is for. Required by FHIR.
        billable_period: Period the claim covers.
        created: When the claim was created, as a ``dateTime``. Required by FHIR.
        enterer: Who entered the claim.
        insurer: The target payer.
        provider: The submitting provider or organization. Required by FHIR.
        priority: Urgency of processing. Required by FHIR.
        referral: Referral authorizing the requested service.
        facility: Where the service will be or was provided.
        care_team: Providers involved in the requested care.
        supporting_info: Evidence supporting the request.
        diagnosis: Diagnoses justifying the requested services.
        procedure: Procedures relevant to the request.
        insurance: Coverages the request is submitted against. Required by FHIR.
        item: The products and services being requested.
        total: Total value of all items.
    """

    resource_type: Literal["Claim"] = "Claim"
    identifier: list[Identifier] | None = None
    status: FinancialResourceStatus
    type: CodeableConcept
    sub_type: CodeableConcept | None = None
    use: ClaimUse
    patient: Reference
    billable_period: Period | None = None
    created: str
    enterer: Reference | None = None
    insurer: Reference | None = None
    provider: Reference
    priority: CodeableConcept
    referral: Reference | None = None
    facility: Reference | None = None
    care_team: list[ClaimCareTeam] | None = None
    supporting_info: list[ClaimSupportingInfo] | None = None
    diagnosis: list[ClaimDiagnosis] | None = None
    procedure: list[ClaimProcedure] | None = None
    insurance: list[ClaimInsurance]
    item: list[ClaimItem] | None = None
    total: Money | None = None

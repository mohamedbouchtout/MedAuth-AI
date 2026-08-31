"""The FHIR R4 Coverage resource.

Coverage is what makes the RAG query answerable: the payer and plan type here
select which insurance policy set applies, and together with the encounter's
state and the ordered CPT code they form the
``rag:{payer}:{plan_type}:{state}:{cpt_code}`` cache key described in CLAUDE.md.

**The ``state`` segment does not come from here, and it is not the patient's.**
It is the site of care, read from the encounter's ``Location``/``Organization``
address — see ``fhir_types.location`` and TASK-052b. Two of the four segments
come off this resource; the third comes off the encounter.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import DomainResource, FHIRBase
from .codes import FinancialResourceStatus
from .datatypes import CodeableConcept, Identifier, Period, Reference


class CoverageClass(FHIRBase):
    """One classification of the coverage — group, plan, subplan and similar.

    The plan type that selects a payer policy usually arrives here, as the entry
    whose ``type`` coding is ``plan``.

    Attributes:
        type: Which kind of classification this entry is. Required by FHIR.
        value: The identifier or code for that classification. Required by FHIR.
        name: Human-readable name for it.
    """

    type: CodeableConcept
    value: str
    name: str | None = None


class Coverage(DomainResource):
    """Insurance or medical plan coverage for a patient.

    The ``class`` element is named ``coverage_class`` in Python because ``class``
    is a reserved word; its alias is ``class``, matching the wire format.

    Attributes:
        identifier: The member id and other business identifiers.
        status: Lifecycle state of the coverage record. Required by FHIR.
        type: Type of coverage — the plan category, e.g. PPO or HMO.
        policy_holder: Owner of the policy.
        subscriber: The person the policy is issued to.
        subscriber_id: The subscriber's id with the payer. PHI.
        beneficiary: The covered patient. Required by FHIR.
        dependent: Dependent number under the policy.
        relationship: The beneficiary's relationship to the subscriber.
        period: When the coverage is in force.
        payor: The insurers responsible for payment. Required by FHIR — this is the
            payer whose policies the RAG query is scoped to.
        coverage_class: Group, plan and subplan classifications. Serialized as
            ``class``.
        order: Relative order of this coverage when several apply.
        network: The insurer's network the patient is in, which changes criteria on
            many policies.
    """

    resource_type: Literal["Coverage"] = "Coverage"
    identifier: list[Identifier] | None = None
    status: FinancialResourceStatus
    type: CodeableConcept | None = None
    policy_holder: Reference | None = None
    subscriber: Reference | None = None
    subscriber_id: str | None = None
    beneficiary: Reference
    dependent: str | None = None
    relationship: CodeableConcept | None = None
    period: Period | None = None
    payor: list[Reference]
    coverage_class: list[CoverageClass] | None = Field(default=None, alias="class")
    order: int | None = None
    network: str | None = None

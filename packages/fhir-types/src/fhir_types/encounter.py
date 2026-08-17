"""The FHIR R4 Encounter resource.

The encounter is the anchor for everything this platform produces: the ambient
audio session, the generated SOAP note, and any prior authorization that comes out
of it all hang off one Encounter id.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import DomainResource, FHIRBase
from .codes import EncounterStatus
from .datatypes import CodeableConcept, Coding, Identifier, Period, Reference


class EncounterParticipant(FHIRBase):
    """A practitioner or other person involved in the encounter.

    Attributes:
        type: The role the participant played.
        period: The span of the encounter they were present for.
        individual: Reference to the Practitioner, PractitionerRole or RelatedPerson.
    """

    type: list[CodeableConcept] | None = None
    period: Period | None = None
    individual: Reference | None = None


class EncounterDiagnosis(FHIRBase):
    """A diagnosis relevant to the encounter.

    Attributes:
        condition: Reference to the Condition or Procedure. Required by FHIR.
        use: Role the diagnosis played — admission, discharge, billing, and so on.
        rank: Ranking among the encounter's diagnoses, 1 being primary.
    """

    condition: Reference
    use: CodeableConcept | None = None
    rank: int | None = None


class Encounter(DomainResource):
    """An interaction between a patient and one or more healthcare providers.

    The ``class`` element is named ``encounter_class`` in Python because ``class``
    is a reserved word. Its alias is ``class``, so both wire formats and
    keyword construction work: ``Encounter(encounter_class=...)`` and
    ``Encounter.model_validate({"class": ...})``.

    Attributes:
        identifier: Business identifiers for the encounter.
        status: Where the encounter is in its lifecycle. Required by FHIR.
        encounter_class: Classification — ambulatory, inpatient, virtual. Serialized
            as ``class``.
        type: Specific kind of encounter.
        service_type: Broad category of service performed.
        priority: Urgency of the encounter.
        subject: The patient present at the encounter.
        participant: Practitioners and others involved.
        period: Start and end of the encounter.
        reason_code: Coded reason the encounter took place.
        reason_reference: Reason expressed as a reference to another resource.
        diagnosis: Diagnoses relevant to this encounter.
        service_provider: Organization responsible for the encounter.
        part_of: Encounter this one is a part of.
    """

    resource_type: Literal["Encounter"] = "Encounter"
    identifier: list[Identifier] | None = None
    status: EncounterStatus
    encounter_class: Coding | None = Field(default=None, alias="class")
    type: list[CodeableConcept] | None = None
    service_type: CodeableConcept | None = None
    priority: CodeableConcept | None = None
    subject: Reference | None = None
    participant: list[EncounterParticipant] | None = None
    period: Period | None = None
    reason_code: list[CodeableConcept] | None = None
    reason_reference: list[Reference] | None = None
    diagnosis: list[EncounterDiagnosis] | None = None
    service_provider: Reference | None = None
    part_of: Reference | None = None

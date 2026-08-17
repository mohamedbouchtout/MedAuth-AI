"""The FHIR R4 Patient resource.

Every field on this resource is PHI. Nothing here may reach a log line; services
reading a Patient must record the access through hipaa-logger's ``audit_log()``.
"""

from __future__ import annotations

from typing import Literal

from .base import DomainResource, FHIRBase
from .codes import AdministrativeGender
from .datatypes import Address, CodeableConcept, ContactPoint, HumanName, Identifier, Reference


class PatientCommunication(FHIRBase):
    """A language the patient can communicate in.

    Attributes:
        language: The language, coded per BCP-47.
        preferred: True when this is the patient's preferred language.
    """

    language: CodeableConcept
    preferred: bool | None = None


class Patient(DomainResource):
    """Demographics and other administrative information about an individual.

    ``deceased`` is a FHIR choice element: a server sends either
    ``deceasedBoolean`` or ``deceasedDateTime``, never both. Both are modelled and
    both are optional, so a caller checks whichever arrived.

    Attributes:
        identifier: Business identifiers, including the MRN.
        active: Whether the record is in active use.
        name: Names by which the patient is known.
        telecom: Contact details.
        gender: Administrative gender, used for record matching — this is not a
            statement about the patient's gender identity, which FHIR carries as an
            extension rather than in this element.
        birth_date: Date of birth, as a ``date`` string.
        deceased_boolean: Whether the patient is deceased, when no date is known.
        deceased_date_time: Date and time of death, when known.
        address: Addresses for the patient. The state here selects which payer
            policy set applies during a prior authorization check.
        marital_status: Marital or civil status.
        communication: Languages the patient can communicate in.
        general_practitioner: The patient's primary care providers.
        managing_organization: Organization that is custodian of the record.
    """

    resource_type: Literal["Patient"] = "Patient"
    identifier: list[Identifier] | None = None
    active: bool | None = None
    name: list[HumanName] | None = None
    telecom: list[ContactPoint] | None = None
    gender: AdministrativeGender | None = None
    birth_date: str | None = None
    deceased_boolean: bool | None = None
    deceased_date_time: str | None = None
    address: list[Address] | None = None
    marital_status: CodeableConcept | None = None
    communication: list[PatientCommunication] | None = None
    general_practitioner: list[Reference] | None = None
    managing_organization: Reference | None = None

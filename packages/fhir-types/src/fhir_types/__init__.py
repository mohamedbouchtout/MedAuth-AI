"""FHIR R4 (4.0.1) resource models shared across services.

Seven resources are modelled — Patient, Encounter, Condition, Coverage,
MedicationRequest, DocumentReference and Claim — together with the datatypes they
are built from. These are the resources listed in CLAUDE.md as the ones this
project reads and writes.

This package is deliberately *not* a complete R4 implementation. Each resource
carries the elements this platform actually uses, and ``extra="allow"`` on the base
model means everything else a server sends survives a validate/dump round trip
untouched. Adding an element later is additive, never breaking.

Every model here is immutable. Build a modified copy with
``resource.model_copy(update={...})`` rather than assigning to a field.

Serialize with ``by_alias=True`` — FHIR element names are camelCase and a server
rejects the snake_case Python field names::

    patient = Patient.model_validate(payload)       # accepts camelCase from the EHR
    body = patient.model_dump(by_alias=True, exclude_none=True)

``exclude_none=True`` matters as much as the alias: FHIR has no concept of a null
element, and sending ``"birthDate": null`` is a validation error on most servers.

TypeScript mirrors of every type here live in ``typescript/src/`` and are checked
against these models by ``tests/unit/test_typescript_parity.py``.

**PHI:** every resource in this package carries patient data. None of them log
anything, but a service that reads one must record the access through
``audit_log()`` from hipaa-logger, and none of these values may reach stdout.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from .base import DomainResource, FHIRBase, Meta
from .claim import (
    Claim,
    ClaimCareTeam,
    ClaimDiagnosis,
    ClaimInsurance,
    ClaimItem,
    ClaimProcedure,
    ClaimSupportingInfo,
)
from .codes import (
    AddressType,
    AddressUse,
    AdministrativeGender,
    ClaimUse,
    CompositionStatus,
    ContactPointSystem,
    ContactPointUse,
    DocumentReferenceStatus,
    EncounterStatus,
    FinancialResourceStatus,
    IdentifierUse,
    MedicationRequestIntent,
    MedicationRequestStatus,
    NameUse,
    QuantityComparator,
    RequestPriority,
)
from .condition import Condition
from .coverage import Coverage, CoverageClass
from .datatypes import (
    Address,
    Annotation,
    Attachment,
    CodeableConcept,
    Coding,
    ContactPoint,
    HumanName,
    Identifier,
    Money,
    Period,
    Quantity,
    Reference,
)
from .document_reference import (
    DocumentReference,
    DocumentReferenceContent,
    DocumentReferenceContext,
)
from .encounter import Encounter, EncounterDiagnosis, EncounterParticipant
from .medication_request import Dosage, DosageDoseAndRate, MedicationRequest
from .patient import Patient, PatientCommunication

FHIR_VERSION = "4.0.1"
"""The FHIR release these models target. See CLAUDE.md — R4, not R4B or R5."""

AnyResource = Annotated[
    Claim | Condition | Coverage | DocumentReference | Encounter | MedicationRequest | Patient,
    Field(discriminator="resource_type"),
]
"""Any resource this package models, narrowed by ``resourceType``.

Use with a ``TypeAdapter`` to parse a resource whose type is not known ahead of
time, such as an entry pulled out of a search Bundle::

    from pydantic import TypeAdapter
    resource = TypeAdapter(AnyResource).validate_python(entry["resource"])

An unmodelled resource type raises rather than validating as the wrong shape.
"""

__all__ = [
    "FHIR_VERSION",
    "Address",
    "AddressType",
    "AddressUse",
    "AdministrativeGender",
    "Annotation",
    "AnyResource",
    "Attachment",
    "Claim",
    "ClaimCareTeam",
    "ClaimDiagnosis",
    "ClaimInsurance",
    "ClaimItem",
    "ClaimProcedure",
    "ClaimSupportingInfo",
    "ClaimUse",
    "CodeableConcept",
    "Coding",
    "CompositionStatus",
    "Condition",
    "ContactPoint",
    "ContactPointSystem",
    "ContactPointUse",
    "Coverage",
    "CoverageClass",
    "DocumentReference",
    "DocumentReferenceContent",
    "DocumentReferenceContext",
    "DocumentReferenceStatus",
    "DomainResource",
    "Dosage",
    "DosageDoseAndRate",
    "Encounter",
    "EncounterDiagnosis",
    "EncounterParticipant",
    "EncounterStatus",
    "FHIRBase",
    "FinancialResourceStatus",
    "HumanName",
    "Identifier",
    "IdentifierUse",
    "MedicationRequest",
    "MedicationRequestIntent",
    "MedicationRequestStatus",
    "Meta",
    "Money",
    "NameUse",
    "Patient",
    "PatientCommunication",
    "Period",
    "Quantity",
    "QuantityComparator",
    "Reference",
    "RequestPriority",
]

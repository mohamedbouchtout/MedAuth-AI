"""Closed FHIR R4 value sets, as ``Literal`` aliases.

Only *required*-binding value sets live here — the ones where FHIR fixes the legal
codes and a server may not extend them. Fields with an extensible or example
binding (diagnosis codes, encounter type, coverage type) stay ``CodeableConcept``,
because constraining those would reject valid payloads from real EHRs.

Every alias in this module has a same-named counterpart in ``typescript/src/codes.ts``.
``tests/unit/test_typescript_parity.py`` compares the two member-for-member, so a
code added on one side and not the other fails CI rather than surfacing as a
validation error against a live EHR.
"""

from __future__ import annotations

from typing import Literal

AdministrativeGender = Literal["male", "female", "other", "unknown"]
"""Patient.gender — https://hl7.org/fhir/R4/valueset-administrative-gender.html"""

EncounterStatus = Literal[
    "planned",
    "arrived",
    "triaged",
    "in-progress",
    "onleave",
    "finished",
    "cancelled",
    "entered-in-error",
    "unknown",
]
"""Encounter.status — https://hl7.org/fhir/R4/valueset-encounter-status.html"""

DocumentReferenceStatus = Literal["current", "superseded", "entered-in-error"]
"""DocumentReference.status — the R4 ``document-reference-status`` value set."""

CompositionStatus = Literal["preliminary", "final", "amended", "entered-in-error"]
"""DocumentReference.docStatus — the underlying composition's lifecycle state."""

FinancialResourceStatus = Literal["active", "cancelled", "draft", "entered-in-error"]
"""Shared by Coverage.status and Claim.status — the R4 ``fm-status`` value set."""

ClaimUse = Literal["claim", "preauthorization", "predetermination"]
"""Claim.use. Prior authorization bundles use ``preauthorization``."""

MedicationRequestStatus = Literal[
    "active",
    "on-hold",
    "cancelled",
    "completed",
    "entered-in-error",
    "stopped",
    "draft",
    "unknown",
]
"""MedicationRequest.status — https://hl7.org/fhir/R4/valueset-medicationrequest-status.html"""

MedicationRequestIntent = Literal[
    "proposal",
    "plan",
    "order",
    "original-order",
    "reflex-order",
    "filler-order",
    "instance-order",
    "option",
]
"""MedicationRequest.intent — https://hl7.org/fhir/R4/valueset-medicationrequest-intent.html"""

RequestPriority = Literal["routine", "urgent", "asap", "stat"]
"""Shared by Encounter.priority-adjacent fields and MedicationRequest.priority."""

IdentifierUse = Literal["usual", "official", "temp", "secondary", "old"]
"""Identifier.use — https://hl7.org/fhir/R4/valueset-identifier-use.html"""

NameUse = Literal[
    "usual",
    "official",
    "temp",
    "nickname",
    "anonymous",
    "old",
    "maiden",
]
"""HumanName.use — https://hl7.org/fhir/R4/valueset-name-use.html"""

ContactPointSystem = Literal[
    "phone",
    "fax",
    "email",
    "pager",
    "url",
    "sms",
    "other",
]
"""ContactPoint.system — https://hl7.org/fhir/R4/valueset-contact-point-system.html"""

ContactPointUse = Literal["home", "work", "temp", "old", "mobile"]
"""ContactPoint.use — https://hl7.org/fhir/R4/valueset-contact-point-use.html"""

AddressUse = Literal["home", "work", "temp", "old", "billing"]
"""Address.use — https://hl7.org/fhir/R4/valueset-address-use.html"""

AddressType = Literal["postal", "physical", "both"]
"""Address.type — https://hl7.org/fhir/R4/valueset-address-type.html"""

QuantityComparator = Literal["<", "<=", ">=", ">"]
"""Quantity.comparator — present only when the value is a bound, not a measurement."""

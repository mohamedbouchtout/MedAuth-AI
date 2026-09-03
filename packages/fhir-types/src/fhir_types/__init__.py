"""FHIR R4 (4.0.1) resource models shared across services.

Eleven resources are modelled — Patient, Encounter, Condition, Coverage,
MedicationRequest, DocumentReference, Claim, Bundle and ClaimResponse, which are
the ones CLAUDE.md lists as what this project reads and writes, plus ``Location``
and ``Organization``. Those two are here because TASK-052b needs them: an
encounter's site of care is what selects which payer policy applies, and it is
read from ``Encounter.location`` -> ``Location.address.state``, falling back to
``Encounter.serviceProvider`` -> ``Organization.address``. Neither is a resource
this platform writes.

``Bundle`` and ``ClaimResponse`` are what Da Vinci PAS actually exchanges
(TASK-004b): ``Claim/$submit`` takes a bundle carrying a Claim plus everything it
references, and answers with one carrying a ClaimResponse. A bundle from outside
holds resource types this package does not model, so ``Bundle.entry.resource`` is
``AnyResourceOrUnknown`` rather than ``AnyResource`` — see ``UnknownResource``.

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

from typing import Annotated, Any, get_args

from pydantic import Discriminator, Field, Tag

from .base import DomainResource, FHIRBase, Meta
from .bundle import (
    Bundle,
    BundleEntry,
    BundleEntryRequest,
    BundleEntryResponse,
    BundleEntrySearch,
    BundleLink,
)
from .claim import (
    Claim,
    ClaimCareTeam,
    ClaimDiagnosis,
    ClaimInsurance,
    ClaimItem,
    ClaimProcedure,
    ClaimSupportingInfo,
)
from .claim_response import (
    ClaimResponse,
    ClaimResponseAdjudication,
    ClaimResponseError,
    ClaimResponseInsurance,
    ClaimResponseItem,
    ClaimResponseProcessNote,
    ClaimResponseTotal,
)
from .codes import (
    AddressType,
    AddressUse,
    AdministrativeGender,
    BundleType,
    ClaimUse,
    CompositionStatus,
    ContactPointSystem,
    ContactPointUse,
    DocumentReferenceStatus,
    EncounterLocationStatus,
    EncounterStatus,
    FinancialResourceStatus,
    HTTPVerb,
    IdentifierUse,
    LocationMode,
    LocationStatus,
    MedicationRequestIntent,
    MedicationRequestStatus,
    NameUse,
    NoteType,
    QuantityComparator,
    RemittanceOutcome,
    RequestPriority,
    SearchEntryMode,
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
from .encounter import (
    Encounter,
    EncounterDiagnosis,
    EncounterLocation,
    EncounterParticipant,
)
from .location import Location, LocationPosition
from .medication_request import Dosage, DosageDoseAndRate, MedicationRequest
from .organization import Organization
from .patient import Patient, PatientCommunication

FHIR_VERSION = "4.0.1"
"""The FHIR release these models target. See CLAUDE.md — R4, not R4B or R5."""

AnyResource = Annotated[
    Bundle
    | Claim
    | ClaimResponse
    | Condition
    | Coverage
    | DocumentReference
    | Encounter
    | Location
    | MedicationRequest
    | Organization
    | Patient,
    Field(discriminator="resource_type"),
]
"""Any resource this package models, narrowed by ``resourceType``.

Use with a ``TypeAdapter`` to parse a resource whose type is known to be one of
these::

    from pydantic import TypeAdapter
    coverage = TypeAdapter(AnyResource).validate_python(payload)

An unmodelled resource type raises rather than validating as the wrong shape.
That is what a caller wants when it knows what it asked for, and it is the wrong
behaviour for a bundle entry — use ``AnyResourceOrUnknown`` there.
"""


class UnknownResource(FHIRBase):
    """A resource of a type this package does not model, kept exactly as it came.

    A PAS response bundle carries referenced resources, and "referenced" is open:
    ``Practitioner``, ``PractitionerRole``, ``Task`` and ``OperationOutcome`` are
    all conformant there and none of them is modelled here. Typed as
    ``AnyResource`` those raise, and a perfectly valid payer response would look
    like a malformed one.

    So this model asserts nothing about the payload beyond it having a
    ``resourceType``. It **declares no elements at all** — every element arrives
    through ``extra="allow"`` and dumps back unchanged, which is the honest
    representation of a shape this package does not know. Nothing is coerced into
    a modelled resource and nothing is discarded.

    Having no declared fields also keeps it out of the TypeScript parity suite,
    which mirrors elements and has none here to mirror. ``typescript/src/bundle.ts``
    carries the equivalent open type by hand.

    Read the type through :attr:`resource_type`; read anything else through
    ``model_extra``::

        if isinstance(entry.resource, UnknownResource):
            resource_type = entry.resource.resource_type
    """

    @property
    def resource_type(self) -> str | None:
        """The ``resourceType`` the payload declared, or ``None`` if it had none.

        A property rather than a field: declaring it would make this look like a
        resource this package models, and would put it in the parity suite and in
        ``AnyResource``'s coverage test as though it were one.
        """
        extra = self.model_extra or {}
        value = extra.get("resourceType", extra.get("resource_type"))
        return value if isinstance(value, str) else None


_MODELLED_RESOURCE_TYPES = frozenset(
    resource.model_fields["resource_type"].default
    for resource in get_args(get_args(AnyResource)[0])
)
"""The ``resourceType`` values ``AnyResource`` can narrow, read off the union.

Derived rather than written out, so a resource added to the union above cannot be
left out of the narrowing below and silently start parsing as an
``UnknownResource``.
"""


def _modelled_or_unknown(value: Any) -> str | None:
    """Route a bundle entry by its ``resourceType`` alone, not by what validates.

    Choosing the branch on the declared type is what keeps a *malformed* modelled
    resource an error: a ``Claim`` missing its required ``status`` raises here,
    rather than falling through to ``UnknownResource`` and being quietly accepted
    as something this package never understood.
    """
    if isinstance(value, dict):
        resource_type = value.get("resourceType", value.get("resource_type"))
    else:
        resource_type = getattr(value, "resource_type", None)

    if not isinstance(resource_type, str):
        return None  # no resourceType at all — Pydantic reports it as untaggable
    return "modelled" if resource_type in _MODELLED_RESOURCE_TYPES else "unknown"


AnyResourceOrUnknown = Annotated[
    Annotated[AnyResource, Tag("modelled")] | Annotated[UnknownResource, Tag("unknown")],
    Discriminator(_modelled_or_unknown),
]
"""``AnyResource``, widened to tolerate a resource type this package does not model.

This is what ``Bundle.entry.resource`` is, and it is the right type for anything
parsing a bundle that came from outside — a payer's PAS response above all. A
modelled type narrows exactly as ``AnyResource`` does, including raising when the
payload is malformed; an unmodelled type becomes an ``UnknownResource`` with every
element intact. A payload with no ``resourceType`` raises either way.

Generating a JSON schema for a model that holds one — ``Bundle.model_json_schema()``
— emits a ``PydanticJsonSchemaWarning`` naming a skipped discriminator. That is a
limitation of describing a *recursive* tagged union in JSON Schema (a Bundle entry
can hold a Bundle), not a sign that the discriminator was skipped at validation
time. Validation is unaffected, and ``tests/unit/test_models.py`` asserts the
narrowing — modelled, unmodelled, malformed and nested — directly.
"""

# ``BundleEntry`` and ``BundleEntryResponse`` annotate their resource elements with
# ``AnyResourceOrUnknown``, which cannot exist until every resource — ``Bundle``
# included — has been imported. They are declared there as forward references and
# resolved here, the same arrangement as ``Identifier.model_rebuild()`` in
# ``datatypes``, one module further apart.
BundleEntry.model_rebuild()
BundleEntryResponse.model_rebuild()


__all__ = [
    "FHIR_VERSION",
    "Address",
    "AddressType",
    "AddressUse",
    "AdministrativeGender",
    "Annotation",
    "AnyResource",
    "AnyResourceOrUnknown",
    "Attachment",
    "Bundle",
    "BundleEntry",
    "BundleEntryRequest",
    "BundleEntryResponse",
    "BundleEntrySearch",
    "BundleLink",
    "BundleType",
    "Claim",
    "ClaimCareTeam",
    "ClaimDiagnosis",
    "ClaimInsurance",
    "ClaimItem",
    "ClaimProcedure",
    "ClaimResponse",
    "ClaimResponseAdjudication",
    "ClaimResponseError",
    "ClaimResponseInsurance",
    "ClaimResponseItem",
    "ClaimResponseProcessNote",
    "ClaimResponseTotal",
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
    "EncounterLocation",
    "EncounterLocationStatus",
    "EncounterParticipant",
    "EncounterStatus",
    "FHIRBase",
    "FinancialResourceStatus",
    "HTTPVerb",
    "HumanName",
    "Identifier",
    "IdentifierUse",
    "Location",
    "LocationMode",
    "LocationPosition",
    "LocationStatus",
    "MedicationRequest",
    "MedicationRequestIntent",
    "MedicationRequestStatus",
    "Meta",
    "Money",
    "NameUse",
    "NoteType",
    "Organization",
    "Patient",
    "PatientCommunication",
    "Period",
    "Quantity",
    "QuantityComparator",
    "Reference",
    "RemittanceOutcome",
    "RequestPriority",
    "SearchEntryMode",
    "UnknownResource",
]

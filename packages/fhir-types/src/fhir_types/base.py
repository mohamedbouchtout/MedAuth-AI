"""Shared configuration for every FHIR model in this package.

Two decisions are made here and inherited by everything else:

**Field names are snake_case in Python, camelCase on the wire.** FHIR R4 elements
are camelCase (``birthDate``, ``managingOrganization``); Python convention is
snake_case. A camel alias generator bridges the two, so ``Patient(birth_date=...)``
and ``Patient.model_validate({"birthDate": ...})`` both work and
``model_dump(by_alias=True)`` produces spec-shaped JSON. Always dump with
``by_alias=True`` when the result leaves the process — a FHIR server will reject
snake_case element names.

**Unknown elements are kept, not rejected.** A real EHR returns far more of each
resource than this package models, plus vendor extensions (see the Epic and Cerner
adapter notes in CLAUDE.md). ``extra="allow"`` means an unmodelled element survives
a validate/dump round trip instead of being silently dropped or raising. The cost is
that a misspelled field is accepted as an extra rather than flagged — worth it,
because dropping data on a resource we later write back to the EHR is the worse
failure.

Nothing in this package is PHI-aware on its own, but every resource here carries
PHI in practice. These are transport shapes only: they hold no logging, and any
service that reads one must still call ``audit_log()`` from hipaa-logger.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class FHIRBase(BaseModel):
    """Base for every FHIR element, datatype and resource in this package."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
        frozen=True,
    )


class Meta(FHIRBase):
    """Resource metadata maintained by the server.

    Only the elements this project reads are modelled. ``security`` and ``tag`` are
    deliberately omitted — nothing here consumes them, and leaving them out keeps
    this module free of any dependency on the datatypes module. ``extra="allow"``
    means a server that sends them still round-trips them unchanged.

    Attributes:
        version_id: Server-assigned version, used for optimistic locking on write.
        last_updated: When the resource was last changed, as an ``instant``.
        source: URI identifying where the resource came from.
        profile: Profile URIs the resource claims to conform to, e.g. US Core.
    """

    version_id: str | None = None
    last_updated: str | None = None
    source: str | None = None
    profile: list[str] | None = None


class DomainResource(FHIRBase):
    """Common header shared by every resource type.

    ``resource_type`` is declared as a ``Literal`` on each subclass rather than
    here, so the resources form a discriminated union — a payload of unknown type
    can be narrowed by that one field.

    Attributes:
        id: Server-assigned logical id, unique within the FHIR server.
        meta: Server-maintained metadata.
        implicit_rules: URI of a ruleset the content is only safe to read under.
        language: Base language of the resource, as a BCP-47 tag.
    """

    id: str | None = None
    meta: Meta | None = None
    implicit_rules: str | None = None
    language: str | None = None

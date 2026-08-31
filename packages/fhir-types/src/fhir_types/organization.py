"""The FHIR R4 Organization resource.

Modelled as the fallback half of TASK-052b's site-of-care rule. An
``Encounter.serviceProvider`` names the organization that performed the service,
so its address answers "where did this take place" whenever the encounter
carries no resolvable ``Location``.

**It is a fallback rather than an equal source, and the order is not arbitrary.**
An organization can span states — a health system's registered address is one
place and its clinics are in several — so ``Organization.address`` is a coarser
answer than the specific room a patient was seen in. Reading the ``Location``
first and this only when that fails keeps the more precise source winning. When
neither resolves, ``state`` stays NULL: the patient's own address is *not* a
third fallback, because the documents say the site of care and a residence is a
different fact that happens to be the same value most of the time.

``address`` is a list here and a single ``Address`` on ``Location`` — that
asymmetry is R4's, not a modelling choice, and the resolution code has to handle
both shapes.
"""

from __future__ import annotations

from typing import Literal

from .base import DomainResource
from .datatypes import Address, CodeableConcept, ContactPoint, Identifier, Reference


class Organization(DomainResource):
    """A grouping of people or organizations with a common purpose.

    Attributes:
        identifier: Business identifiers — an NPI, a tax id.
        active: Whether the organization's record is in active use.
        type: The kind of organization, e.g. a healthcare provider.
        name: The organization's name.
        alias: Other names it has been known by.
        telecom: Contact details.
        address: Postal addresses. A **list** in R4, unlike ``Location.address``.
            Not every entry describes where care happens — a billing address
            routinely sits in another state — so the site-of-care rule picks
            among them rather than taking the first. That choice lives with
            the code that applies it, in ``fhir-integration``.
        part_of: The organization this one belongs to.
    """

    resource_type: Literal["Organization"] = "Organization"
    identifier: list[Identifier] | None = None
    active: bool | None = None
    type: list[CodeableConcept] | None = None
    name: str | None = None
    alias: list[str] | None = None
    telecom: list[ContactPoint] | None = None
    address: list[Address] | None = None
    part_of: Reference | None = None

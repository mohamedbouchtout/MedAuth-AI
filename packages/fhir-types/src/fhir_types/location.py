"""The FHIR R4 Location resource.

Modelled for one reason: it is where an encounter physically happened, and the
site of care is what selects which payer policy applies. TASK-052b establishes
that from the documents themselves — CMS's Medicare Coverage Database says to
search by "the state where the service took place" — so ``Location.address.state``
is the primary source of the ``state`` segment of
``rag:{payer}:{plan_type}:{state}:{cpt_code}``.

Only the elements that answer that question are modelled, plus the two that say
whether the answer is trustworthy. ``mode`` matters more than it looks: a
``kind`` location describes a *class* of place rather than a particular one, so
its address, where it has one at all, belongs to a template and not to anywhere
a service was performed.
"""

from __future__ import annotations

from typing import Literal

from .base import DomainResource, FHIRBase
from .codes import LocationMode, LocationStatus
from .datatypes import Address, CodeableConcept, ContactPoint, Identifier, Reference


class LocationPosition(FHIRBase):
    """The location's absolute geographic position, in WGS84.

    Modelled because Synthea populates it on every generated facility and
    ``extra="allow"`` would otherwise be the only thing carrying it through a
    round trip. Nothing in this platform reads it — the site-of-care rule works
    from the postal address, which is the form a policy's own jurisdiction is
    published in.

    Attributes:
        longitude: Degrees east of the prime meridian. Required by FHIR.
        latitude: Degrees north of the equator. Required by FHIR.
        altitude: Metres above the WGS84 ellipsoid.
    """

    longitude: float
    latitude: float
    altitude: float | None = None


class Location(DomainResource):
    """A physical place where care is delivered.

    Attributes:
        identifier: Business identifiers for the place.
        status: Whether the location record is active. Required binding.
        name: The name the organization calls this place.
        alias: Other names it has been known by.
        description: Free-text description of the place.
        mode: Whether this is a specific place or a class of place. See the
            module docstring — a ``kind`` has no site-of-care address.
        type: The kind of function performed at the location.
        telecom: Contact details for the place.
        address: The postal address. **This is the site-of-care address the
            ``state`` cache-key segment is read from** (TASK-052b), and it is
            singular in R4 — unlike ``Organization.address``, which is a list.
        physical_type: Whether this is a building, a room, a vehicle and so on.
        position: Geographic coordinates.
        managing_organization: The organization responsible for the place.
        part_of: The location this one sits inside.
    """

    resource_type: Literal["Location"] = "Location"
    identifier: list[Identifier] | None = None
    status: LocationStatus | None = None
    name: str | None = None
    alias: list[str] | None = None
    description: str | None = None
    mode: LocationMode | None = None
    type: list[CodeableConcept] | None = None
    telecom: list[ContactPoint] | None = None
    address: Address | None = None
    physical_type: CodeableConcept | None = None
    position: LocationPosition | None = None
    managing_organization: Reference | None = None
    part_of: Reference | None = None

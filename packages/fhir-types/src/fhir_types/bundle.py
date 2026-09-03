"""The FHIR R4 Bundle resource.

Da Vinci PAS moves prior authorizations as bundles, not as bare Claims:
``Claim/$submit`` takes a ``Bundle`` on ``profile-pas-request-bundle`` — a single
Claim plus every resource it references — and answers with a ``Bundle`` on
``profile-pas-response-bundle``, a ``ClaimResponse`` plus its referenced resources.
See TASK-054 for the operation's shape.

**An entry's resource is not typed as ``AnyResource``.** A PAS response bundle
carries referenced resources conformantly including types this package does not
model — ``Practitioner``, ``PractitionerRole``, ``Task``, ``OperationOutcome``.
``AnyResource`` raises on those by design, which is right where a caller knows what
it asked for and wrong here: a perfectly valid payer response would fail to parse
and the failure would read as a malformed response from the payer. So an entry is
``AnyResourceOrUnknown``, which narrows a modelled type exactly as before and keeps
an unmodelled one intact as an ``UnknownResource`` rather than coercing it into a
modelled shape or discarding it. See ``fhir_types.__init__`` for how that union is
built.

``Bundle`` extends ``DomainResource`` for its ``id``/``meta``/``implicitRules``/
``language`` header, which is what this package's base class actually carries. FHIR
itself derives Bundle from ``Resource`` rather than ``DomainResource`` — it has no
``text``, ``contained`` or ``extension`` — and since this package models none of
those three, the two spellings hold the same elements.

``Bundle.signature`` is not modelled. Nothing here signs or verifies a bundle, and
``extra="allow"`` round-trips one a payer sends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from .base import DomainResource, FHIRBase
from .codes import BundleType, HTTPVerb, SearchEntryMode
from .datatypes import Identifier

if TYPE_CHECKING:  # resolved by ``model_rebuild()`` in ``fhir_types.__init__``
    from . import AnyResourceOrUnknown


class BundleLink(FHIRBase):
    """A named link relevant to the bundle or to one entry.

    Attributes:
        relation: The IANA relation name — ``self``, ``next``, ``previous``. A
            plain string in R4, not a coded element. Required by FHIR.
        url: The link's target. Required by FHIR.
    """

    relation: str
    url: str


class BundleEntrySearch(FHIRBase):
    """Why an entry is in a search result. Prohibited by PAS; modelled anyway.

    Attributes:
        mode: Whether the entry matched the search or was pulled in by ``_include``.
        score: Search relevance ranking, 0 to 1.
    """

    mode: SearchEntryMode | None = None
    score: float | None = None


class BundleEntryRequest(FHIRBase):
    """The transaction or batch operation an entry represents.

    Attributes:
        method: The HTTP verb for this entry's operation. Required by FHIR.
        url: The request URL, relative to the server's base. Required by FHIR.
        if_none_match: ``ETag``-based precondition for a conditional read.
        if_modified_since: Timestamp precondition for a conditional read, as an
            ``instant``.
        if_match: ``ETag``-based precondition for a conditional update.
        if_none_exist: Search string for a conditional create.
    """

    method: HTTPVerb
    url: str
    if_none_match: str | None = None
    if_modified_since: str | None = None
    if_match: str | None = None
    if_none_exist: str | None = None


class BundleEntryResponse(FHIRBase):
    """The server's outcome for one transaction or batch entry.

    Attributes:
        status: The HTTP status line for this entry. Required by FHIR.
        location: Where a created resource can be read from.
        etag: The created or updated resource's version, for optimistic locking.
        last_modified: When the resource was changed, as an ``instant``.
        outcome: An ``OperationOutcome`` carrying detail about the operation.
            Unmodelled here, so it arrives as an ``UnknownResource`` with its
            issues intact rather than raising.
    """

    status: str
    location: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    outcome: AnyResourceOrUnknown | None = None


class BundleEntry(FHIRBase):
    """One resource carried by a bundle, with the metadata that frames it.

    Attributes:
        full_url: Absolute URL identifying the resource, and what intra-bundle
            references resolve against. A PAS bundle's Claim points at its Patient
            and Coverage entries through this, so dropping it breaks the request in
            a way the payer reports as a missing resource rather than a bad link.
        resource: The resource itself. An unmodelled type parses as an
            ``UnknownResource`` — see this module's docstring.
        search: Search metadata. Prohibited by PAS.
        request: Transaction or batch operation. Prohibited by PAS.
        response: Transaction or batch outcome. Prohibited by PAS.
        link: Links relevant to this entry alone.
    """

    full_url: str | None = None
    resource: AnyResourceOrUnknown | None = None
    search: BundleEntrySearch | None = None
    request: BundleEntryRequest | None = None
    response: BundleEntryResponse | None = None
    link: list[BundleLink] | None = None


class Bundle(DomainResource):
    """A container for a collection of resources.

    Attributes:
        identifier: Persistent identifier for the bundle, assigned by its author.
        type: What the bundle is for. PAS uses ``collection``. Required by FHIR.
        timestamp: When the bundle was assembled, as an ``instant``.
        total: Number of matches in a ``searchset`` or ``history``. Not a count of
            entries — an ``_include``d resource is an entry and not a match.
        link: Links relevant to the bundle as a whole, e.g. paging.
        entry: The resources carried, each with its own framing metadata.
    """

    resource_type: Literal["Bundle"] = "Bundle"
    identifier: Identifier | None = None
    type: BundleType
    timestamp: str | None = None
    total: int | None = None
    link: list[BundleLink] | None = None
    entry: list[BundleEntry] | None = None

"""Which adapter answers for which EHR, and how the SMART ``iss`` selects one.

Route handlers import exactly two things from this module — ``get_adapter()``
and ``detect_ehr_from_issuer()`` — and never a concrete adapter class. That is
what keeps vendor knowledge inside the adapter layer.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from urllib.parse import urlsplit

import httpx

from .athena import AthenaAdapter
from .base import EHRAdapter
from .cerner import CernerAdapter
from .covermymeds import CoverMyMedsClient
from .ecw import ECWAdapter
from .epic import EpicAdapter
from .modmed import ModMedAdapter

logger = logging.getLogger(__name__)


class EHRType(StrEnum):
    """The EHR vendor keys, as a closed vocabulary rather than a bare string.

    Third time this repository has reached the same conclusion about an
    identifier matched by string equality, after ``payer_vocab``'s canonical
    slugs and ``hipaa_logger.AuditAction``: one spelling, defined once, with
    mypy rejecting an invented value at the call site.

    It matters more here than in an ordinary signature because this value
    **round-trips through Redis** — TASK-051 writes it into
    ``fhir_token:{launch_id}`` at launch and a later request reads it back to
    pick an adapter — so a free-form string would put the write side and the
    read side in two modules with nothing holding them in step. ``StrEnum`` for
    the same reason ``AuditAction`` is one: a member compares equal to its own
    text, so what goes into Redis and comes back out is an ordinary string and
    no serialisation step has to know about the type.

    ``GENERIC`` is an EHR the issuer did not identify; it routes to the standard
    FHIR adapter rather than to a vendor subclass.
    """

    ATHENA = "athena"
    ECW = "ecw"
    MODMED = "modmed"
    CERNER = "cerner"
    EPIC = "epic"
    GENERIC = "generic"


# Host fragments, checked in order. The order is fixed rather than incidental,
# so two readers of detect_ehr_from_issuer() cannot disagree about a host that
# matches two of them:
#
# - "cerner" and "oraclehealth" both mean Cerner. Oracle's acquisition puts both
#   in real issuer URLs during the rename, sometimes in the same host, so they
#   resolve to one key and the ordering changes no outcome — it is written down
#   anyway rather than left to whichever happened to be first.
# - "epic" is last because it is four characters that occur inside ordinary
#   words, so anything more specific gets the first look.
_HOST_FRAGMENTS: tuple[tuple[str, EHRType], ...] = (
    ("eclinicalworks", EHRType.ECW),
    ("cerner", EHRType.CERNER),
    ("oraclehealth", EHRType.CERNER),
    ("athena", EHRType.ATHENA),
    ("modmed", EHRType.MODMED),
    ("epic", EHRType.EPIC),
)

_ADAPTERS: dict[EHRType, type[EHRAdapter]] = {
    EHRType.ATHENA: AthenaAdapter,
    EHRType.ECW: ECWAdapter,
    EHRType.MODMED: ModMedAdapter,
    EHRType.CERNER: CernerAdapter,
    EHRType.EPIC: EpicAdapter,
    EHRType.GENERIC: EHRAdapter,
}


def _issuer_host(iss_url: str) -> str:
    """Pull the lowercase host out of a SMART ``iss``, without its port or credentials.

    An ``iss`` occasionally arrives without a scheme, which leaves ``urlsplit``
    with an empty netloc and the whole value in the path. Falling back to the
    first path segment recovers the host in that case without ever looking at a
    later segment, which is the part matching must not see.

    Args:
        iss_url: The SMART launch ``iss`` parameter.

    Returns:
        The host, lowercased, or an empty string when there is none to find.
    """
    parts = urlsplit(iss_url.strip())
    if parts.hostname:
        return parts.hostname.lower()
    return parts.path.lstrip("/").split("/", 1)[0].split(":", 1)[0].lower()


def detect_ehr_from_issuer(iss_url: str) -> EHRType:
    """Identify the EHR vendor from the SMART launch ``iss`` parameter.

    Matching is case-insensitive and runs against the **host only**. It never
    looks at the path or query string: ``"epic"`` is four characters that occur
    inside ordinary words, so a practice named "Epicenter Orthopedics" in a path
    segment would otherwise select the Epic adapter for a server that is not
    Epic.

    **An unrecognised issuer is not an error.** It resolves to
    ``EHRType.GENERIC`` and logs a WARNING, the same arrangement an unknown
    payer gets in ``packages/payer-vocab``: the launch still works, because an
    EHR we have not seen is usually still a conformant FHIR R4 server, and the
    fact that the name did not line up is visible in the operational trace
    instead of looking like a normal answer. Raising would turn every launch
    from a new EHR into a failed launch.

    Only the host is logged. An ``iss`` can carry launch context in its query
    string, and that does not belong in a log line.

    Args:
        iss_url: The ``iss`` parameter the EHR sent to ``GET /fhir/launch``.

    Returns:
        The vendor key, or ``EHRType.GENERIC`` when no fragment matched.
    """
    host = _issuer_host(iss_url)
    for fragment, ehr_type in _HOST_FRAGMENTS:
        if fragment in host:
            return ehr_type

    logger.warning(
        "Unrecognised SMART issuer host %r — using the standard FHIR adapter. "
        "Add a vendor key to EHRType if this EHR needs its own.",
        host,
    )
    return EHRType.GENERIC


def get_adapter(
    ehr_type: EHRType,
    fhir_base_url: str,
    access_token: str,
    http_client: httpx.AsyncClient,
    covermymeds: CoverMyMedsClient | None = None,
) -> EHRAdapter:
    """Build the adapter for one EHR and one session.

    The signature asks for ``EHRType``, which is what mypy enforces. A valid
    string is coerced at runtime as a backstop rather than an invitation:
    ``ehr_type`` comes back out of Redis as plain text (TASK-051), and a
    ``StrEnum`` member does not hash equal to its own text, so the lookup would
    otherwise miss for exactly the caller this fallback exists for.

    Args:
        ehr_type: The vendor key, from ``detect_ehr_from_issuer()`` or Redis.
        fhir_base_url: Base URL of that EHR's FHIR R4 server.
        access_token: The SMART on FHIR access token for this session.
        http_client: The process-wide HTTP client the adapter makes its FHIR
            calls on. Injected rather than built per adapter — see
            ``EHRAdapter``'s class docstring for why.
        covermymeds: The CoverMyMeds submission path, for the one adapter that
            needs it. **Vendor-specific configuration is passed here rather than
            added to the base adapter's constructor**: this function is the only
            place that knows which class it is building, so it is the only place
            that can hand one vendor something the others have no use for.
            Ignored by every adapter but Athenahealth's, and ``None`` is a real
            state — nothing configured the path — which that adapter reports
            plainly rather than discovering inside an HTTP call.

    Returns:
        The adapter for that vendor, or a plain ``EHRAdapter`` for
        ``EHRType.GENERIC``.

    Raises:
        ValueError: If ``ehr_type`` is not a member of ``EHRType``.
    """
    try:
        key = EHRType(ehr_type)
    except ValueError as exc:
        raise ValueError(
            f"Unknown EHR type {ehr_type!r} — add it to EHRType in "
            "src/adapters/factory.py, with an adapter class behind it."
        ) from exc

    if key is EHRType.ATHENA:
        return AthenaAdapter(
            fhir_base_url=fhir_base_url,
            access_token=access_token,
            http_client=http_client,
            covermymeds=covermymeds,
        )

    return _ADAPTERS[key](
        fhir_base_url=fhir_base_url,
        access_token=access_token,
        http_client=http_client,
    )

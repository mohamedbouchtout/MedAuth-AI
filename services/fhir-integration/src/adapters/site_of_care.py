"""Where an encounter took place — the ``state`` segment of the RAG cache key.

**The site of care is the answer, and it was read out of the documents rather
than reasoned about.** The three candidates were the patient's residence, the
plan's issuing state and the practice location, and they disagree for a patient
treated out of state. TASK-052b settled it against this repository's own corpus:

* CMS's Medicare Coverage Database — the database ``policy-scraper`` scrapes —
  tells the reader to select *"the state where the service took place"*, and
  uses "location of service" twice more in the guidance immediately below.
* A real LCD (L39529) scopes itself by contractor jurisdiction and mentions no
  beneficiary at all; "resides", "rendered" and "place of service" appear
  nowhere in it.
* Aetna's Clinical Policy Bulletins carry no geographic scope of any kind.
* BCBSMA's policies scope by product line and never say "Massachusetts".

So one payer states a rule, it says the site of care, and none of the others
points anywhere else. Where the commercial payers are silent the site-of-care
rule is *carried over* rather than documented — a defensible default, recorded
as one, with the disagreement logged (:func:`log_state_disagreement`) instead of
being invisible.

**The resolution order is Location, then Organization, then nothing.** A
``Location`` is the room the patient was seen in; an ``Organization`` can span
states, so it is the coarser answer and comes second. The patient's own address
is deliberately not a third fallback: a residence is a different fact that
happens to hold the same value most of the time, and reading it when the real
source is missing would turn "we do not know where this happened" into a
confident wrong key. NULL is the honest outcome, and
``resolve_query_parameters()`` already names ``state`` as missing when it is.

Nothing here performs I/O. The fetches live on
:class:`~src.adapters.base.EHRAdapter` so this module stays a set of pure
functions the unit tests can drive directly.
"""

from __future__ import annotations

import logging
from typing import Final
from urllib.parse import urlsplit

from fhir_types import Address, AddressUse, Encounter, Location, Organization, Reference
from payer_vocab import normalize_state

logger = logging.getLogger(__name__)

#: ``Encounter.location.status`` values that mean the patient actually reached
#: the place. ``planned`` is somewhere they were expected and may never have
#: gone; ``reserved`` is a room held for them. Treating either as the site of
#: care would key a policy query on a building the service did not happen in.
#:
#: **An absent status is eligible**, and that is the common case rather than an
#: edge one: Synthea, HAPI's sample data and both public sandboxes all emit
#: ``Encounter.location`` entries with no ``status`` element. Excluding those
#: would leave every real encounter with no location at all.
_PRESENT_LOCATION_STATUSES: Final[frozenset[str]] = frozenset({"active", "completed"})

#: Address uses that never describe where a service was performed. A billing
#: address is where remittance goes and routinely sits in a different state from
#: every clinic it bills for; an ``old`` address described somewhere the
#: organization no longer is. Either one read as a site of care produces a
#: confident wrong state.
_NON_SITE_ADDRESS_USES: Final[frozenset[AddressUse]] = frozenset({"billing", "old"})


def reference_id(reference: Reference | None, resource_type: str) -> str | None:
    """Return the id a reference points at, or None when it cannot be read by id.

    Three reference forms reach this function and only one of them is
    resolvable:

    * ``Location/123`` and its absolute form ``https://host/fhir/Location/123``
      — a plain read, which is what a server returns once resources are stored.
    * ``urn:uuid:...`` — only meaningful inside the Bundle that defines it, and
      this adapter reads one resource at a time.
    * ``Location?identifier=system|value`` — a conditional reference. Synthea's
      generated files use these and HAPI rewrites them at transaction time, so
      one surviving into a stored resource means the server did not resolve it.

    Args:
        reference: The reference element, or None.
        resource_type: The type the reference must name, e.g. ``"Location"``.

    Returns:
        The logical id, or None for an unresolvable form or a reference to a
        different resource type.
    """
    if reference is None or not reference.reference:
        return None

    raw = reference.reference.strip()
    if raw.startswith("urn:"):
        return None
    if "?" in raw:
        return None
    # Strip scheme and host so an absolute reference reads the same as a
    # relative one. ``urlsplit`` leaves a relative reference's path untouched.
    segments = [segment for segment in urlsplit(raw).path.split("/") if segment]
    if len(segments) < 2 or segments[-2] != resource_type:
        return None
    return segments[-1]


def site_location_references(encounter: Encounter) -> list[str]:
    """Return the ``Location`` ids worth reading, in the order to try them.

    Entries the patient never reached are dropped and the rest keep the order
    the encounter listed them in. Several are returned rather than one because
    the first may be a ``Location`` carrying no address at all, and falling
    through to the next location is a better answer than falling through to the
    organization.

    Args:
        encounter: The encounter as the EHR holds it.

    Returns:
        Resolvable ``Location`` ids, possibly empty.
    """
    ids: list[str] = []
    for entry in encounter.location or []:
        if entry.status is not None and entry.status not in _PRESENT_LOCATION_STATUSES:
            continue
        location_id = reference_id(entry.location, "Location")
        if location_id is not None and location_id not in ids:
            ids.append(location_id)
    return ids


def service_provider_reference(encounter: Encounter) -> str | None:
    """Return the ``Organization`` id of the encounter's service provider."""
    return reference_id(encounter.service_provider, "Organization")


def _site_state(addresses: list[Address]) -> str | None:
    """Return the first eligible address's raw ``state``, unnormalized."""
    for address in addresses:
        if address.use in _NON_SITE_ADDRESS_USES:
            continue
        if address.state and address.state.strip():
            return address.state
    return None


def location_state(location: Location) -> str | None:
    """Return a ``Location``'s raw site-of-care state, or None.

    A ``kind`` location is refused: it describes a *class* of place — "a general
    practice room" — so its address, where it has one, belongs to a template
    rather than to anywhere a service was performed.
    """
    if location.mode == "kind":
        logger.info(
            "Skipping a Location with mode='kind' as a site of care: it describes a "
            "class of place, not somewhere a service was performed."
        )
        return None
    return _site_state([location.address] if location.address else [])


def organization_state(organization: Organization) -> str | None:
    """Return an ``Organization``'s raw site-of-care state, or None."""
    return _site_state(list(organization.address or []))


def patient_address_state(addresses: list[Address] | None) -> str | None:
    """Return the patient's raw residence state, for the disagreement check only.

    **Never a source for the cache key.** It exists so a patient treated out of
    state shows up in the operational trace instead of being invisible. A
    ``home`` address wins where one is marked, because a patient can also carry
    a temporary or work address that is not where they live.
    """
    entries = list(addresses or [])
    home = [address for address in entries if address.use == "home"]
    return _site_state(home or entries)


def to_usps_state(raw: str | None) -> str | None:
    """Normalize a raw FHIR state to a two-character USPS code, or None.

    ``payer_vocab.normalize_state`` raises on anything it does not recognise,
    which is right on the ingestion side — a code that reached the Qdrant
    payload unrecognised would match nothing and look like an empty corpus. Here
    the same failure has a better answer than an exception: a server that spells
    its state ``"Massachusetts"`` should leave the column NULL and say so, not
    fail the SMART launch. NULL is already handled downstream; a raw or wrong
    value is not.

    Args:
        raw: The state as the resource spelled it, or None.

    Returns:
        The USPS code, or None when there was nothing to normalize or the value
        is not a state code this vocabulary knows.
    """
    if raw is None:
        return None
    try:
        return normalize_state(raw)
    except ValueError:
        logger.warning(
            "A site-of-care address carried %r, which is not a USPS or CMS state code. "
            "Leaving the encounter's state NULL rather than storing it raw — a value "
            "the policy corpus is not indexed under would match nothing and read as "
            "'no policy found'.",
            raw,
        )
        return None


def log_state_disagreement(site_state: str | None, patient_state: str | None) -> None:
    """Log at WARNING when the site of care and the patient's residence differ.

    The site-of-care answer still goes out — it is what the documents say. This
    only makes the disagreement visible, in the same spirit as an unrecognised
    payer slug in ``payer_vocab`` and as ``requires_manual_confirmation``: the
    commercial half of the corpus states no rule either way, so a case where the
    two candidate answers differ is worth being able to find later.

    **Only the two state codes are logged, and never an identifier.** A state on
    its own is not one of HIPAA Safe Harbor's geographic identifiers — those
    begin below the state level — and nothing here names the patient, the
    encounter or the launch, so no line this writes links a state to a person.
    """
    if site_state is None or patient_state is None or site_state == patient_state:
        return
    logger.warning(
        "Site of care (%s) and the patient's address (%s) are in different states. "
        "The policy query uses the site of care, per TASK-052b — CMS scopes coverage "
        "documents by where the service took place. Logged because the commercial "
        "payers in the corpus state no rule either way.",
        site_state,
        patient_state,
    )

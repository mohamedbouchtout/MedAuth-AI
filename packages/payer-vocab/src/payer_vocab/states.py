"""Turning a jurisdiction's state code into the two-character code we match on.

This is the payer problem one column over, and it bites for the same reason: a
policy is stored under whatever code its source used, a query arrives with
whatever code the encounter carries, and the two are compared by exact equality.
A FHIR ``Coverage`` will say ``NY``. CMS's Medicare Coverage Database will say
``DN``, ``QN`` or ``UN`` — New York downstate, Queens and upstate, three
Medicare Administrative Contractor jurisdictions inside one state. Neither
system is wrong; they are just not the same vocabulary, and nothing in between
was translating.

CMS's ``state_lookup`` table also carries ``NF``/``SF`` for northern and
southern California, ``EM``/``WM`` for Missouri, and ``CNMI`` for the Northern
Mariana Islands — four characters, which does not even fit the ``CHAR(2)``
column it would be written to. All of them are normalised here, at ingestion,
so that everything downstream compares USPS codes against USPS codes.
"""

from __future__ import annotations

from typing import Final

#: The two-character codes this system recognises: the fifty states, DC, and the
#: five territories with their own USPS codes. A code outside this set is a bug
#: at the caller rather than a jurisdiction we have not met.
USPS_STATE_CODES: Final[frozenset[str]] = frozenset(
    (
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD "
        "MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC "
        "SD TN TX UT VT VA WA WV WI WY DC AS GU MP PR VI"
    ).split()
)

#: CMS jurisdiction codes that are not USPS state codes, mapped to the state
#: they sit inside. Taken from the ``state_lookup`` table in the Medicare
#: Coverage Database export; the descriptions there name the parent state
#: outright ("New York - Downstate", "California - Northern").
CMS_JURISDICTION_CODES: Final[dict[str, str]] = {
    "DN": "NY",  # New York - Downstate
    "QN": "NY",  # New York - Queens
    "UN": "NY",  # New York - Upstate
    "NF": "CA",  # California - Northern
    "SF": "CA",  # California - Southern
    "EM": "MO",  # Missouri - Northeastern & Southern
    "WM": "MO",  # Missouri - Northwestern
    "CNMI": "MP",  # Northern Mariana Islands — four characters at the source
}


def normalize_state(raw: str) -> str:
    """Return the USPS code for a state or jurisdiction code from any source.

    Args:
        raw: A state code — ``"ma"`` from a form, ``"MA"`` from a FHIR resource,
            or one of CMS's jurisdiction codes such as ``"DN"`` or ``"CNMI"``.

    Returns:
        The two-character USPS code. Sub-state Medicare jurisdictions collapse
        to their parent state, because that is the granularity every other part
        of the system — encounters, Coverage resources, the Qdrant filter —
        works at.

    Raises:
        ValueError: The code is neither a USPS code nor a CMS jurisdiction code.
            Failing here is the point: a code that silently reached the ``state``
            payload would match nothing at query time and look like an empty
            corpus.
    """
    code = raw.strip().upper()
    if code in CMS_JURISDICTION_CODES:
        return CMS_JURISDICTION_CODES[code]
    if code in USPS_STATE_CODES:
        return code
    raise ValueError(f"Unrecognised state or jurisdiction code: {raw!r}")


def normalize_states(raw_codes: list[str]) -> list[str]:
    """Return the sorted, de-duplicated USPS codes for a MAC jurisdiction.

    A Medicare LCD covers every state in the contractor jurisdiction that issued
    it — a median of twelve, and up to forty-eight. Two of those can collapse
    onto one state (``DN`` and ``UN`` are both New York), hence the de-duplication;
    the sort makes the stored list stable, so a policy whose jurisdiction has not
    changed produces the same list on every scrape.

    Args:
        raw_codes: The jurisdiction's codes, as the source spells them.

    Returns:
        The distinct USPS codes, sorted.

    Raises:
        ValueError: Any code is unrecognised — see :func:`normalize_state`.
    """
    return sorted({normalize_state(code) for code in raw_codes})

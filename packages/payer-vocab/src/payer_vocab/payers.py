"""Turning a payer's name, however it was spelled, into one canonical slug.

Two layers, in this order:

1. **A deterministic slug.** Casefold, drop punctuation and corporate suffixes,
   collapse whitespace, join with hyphens. This is what guarantees that two
   spellings of one name cannot become two payers — it needs no curation and
   never goes stale.
2. **An alias table**, consulted on the slug, for the cases step 1 cannot reach.
   No amount of string manipulation turns "Medicare Part B" into the same token
   as "CMS"; only knowing that they name the same payer does. That knowledge is
   curated data, kept in :data:`PAYER_ALIASES`, and the way to teach the system
   a new payer is to add a row there rather than to make the slug function
   cleverer.

An unrecognised payer is deliberately *not* an error. It slugs, it queries, and
it retrieves nothing if we hold no policies for it — which is the honest answer.
:func:`is_known_payer` exists so the caller can log that case at WARNING and
keep "the name did not line up" distinguishable from "this payer has no policy
on file". Those two look identical from the outside, and telling them apart
after the fact is exactly what this package was written to make possible.
"""

from __future__ import annotations

import re
from typing import Final

#: Legal and structural suffixes that carry no identity. "Aetna Inc." and
#: "Aetna" are the same payer; the suffix only varies by who typed it.
_NOISE_WORDS: Final = frozenset(
    {
        "co",
        "company",
        "corp",
        "corporation",
        "group",
        "holdings",
        "inc",
        "incorporated",
        "llc",
        "lp",
        "ltd",
        "plc",
        "the",
    }
)

_SEPARATORS: Final = re.compile(r"[^a-z0-9]+")


def slugify_payer(raw: str) -> str:
    """Return the deterministic slug for a payer name, before alias resolution.

    Exposed for tests and for callers that want the mechanical half on its own.
    Application code should call :func:`normalize_payer`, which also applies the
    alias table.

    Args:
        raw: A payer name as a human or an upstream system spelled it.

    Returns:
        A lowercase hyphenated slug, or the empty string if the name held no
        alphanumeric characters at all.
    """
    words = [word for word in _SEPARATORS.split(raw.strip().casefold()) if word]
    meaningful = [word for word in words if word not in _NOISE_WORDS]
    # A name made entirely of noise words ("The Group") is not improved by
    # reducing it to nothing; keep the original words in that case.
    return "-".join(meaningful or words)


#: Slug-to-canonical-slug mapping for names the slug function cannot unify.
#:
#: Keyed on the *slug* rather than the raw string, so every casing and
#: punctuation variant of an alias resolves through a single entry: "Medicare
#: Part B", "medicare part b" and "MEDICARE PART B" all arrive here as
#: ``medicare-part-b``.
PAYER_ALIASES: Final[dict[str, str]] = {
    # Traditional Medicare. Everything CMS publishes — every NCD, every LCD —
    # ingests under this slug, so a Coverage resource naming any part of the
    # program has to land on it too.
    "cms": "cms-medicare",
    "medicare": "cms-medicare",
    "medicare-part-a": "cms-medicare",
    "medicare-part-b": "cms-medicare",
    "medicare-part-a-b": "cms-medicare",
    "original-medicare": "cms-medicare",
    "traditional-medicare": "cms-medicare",
    "centers-for-medicare-medicaid-services": "cms-medicare",
    "centers-for-medicare-and-medicaid-services": "cms-medicare",
    # Medicare Advantage is *not* traditional Medicare: plans set their own
    # prior-authorization rules on top of the national ones, and TASK-015 routes
    # them down the Da Vinci CRD path rather than to CMS policy text.
    "medicare-advantage": "medicare-advantage",
    "medicare-part-c": "medicare-advantage",
    # Commercial payers whose corporate names appear in more than one form.
    "aetna-health": "aetna",
    "aetna-life-insurance": "aetna",
    "cvs-aetna": "aetna",
    "united-healthcare": "unitedhealthcare",
    "united-health-care": "unitedhealthcare",
    "uhc": "unitedhealthcare",
    "unitedhealth": "unitedhealthcare",
    "bcbs": "blue-cross-blue-shield",
    "blue-cross": "blue-cross-blue-shield",
    "blue-shield": "blue-cross-blue-shield",
    "bluecross-blueshield": "blue-cross-blue-shield",
    "anthem": "anthem-bcbs",
    "anthem-blue-cross-blue-shield": "anthem-bcbs",
    "cigna-healthcare": "cigna",
    "humana-health-plan": "humana",
}

#: Payers we expect to see and hold or plan to hold policies for. A slug outside
#: this set still works; it is just not one anybody has curated.
KNOWN_PAYERS: Final[frozenset[str]] = frozenset(
    {
        "cms-medicare",
        "medicare-advantage",
        "medicaid",
        "aetna",
        "anthem-bcbs",
        "blue-cross-blue-shield",
        "cigna",
        "humana",
        "unitedhealthcare",
    }
)


def normalize_payer(raw: str) -> str:
    """Return the canonical slug a payer's policies are stored and matched under.

    This is the value written to Qdrant payloads and ``insurance_policies``, the
    value the retrieval filter matches on, and the ``{payer}`` segment of the
    ``rag:{payer}:{plan_type}:{state}:{cpt_code}`` cache key. Ingestion and query
    must both call it, or they disagree about which payer they mean and the
    disagreement surfaces as an empty retrieval rather than as an error.

    Args:
        raw: A payer name from anywhere — an ingest form field, a FHIR
            ``Coverage`` resource's free-text display, a seed script.

    Returns:
        The canonical slug. Names the alias table does not recognise return
        their bare slug, which is a usable answer and not an error; pair this
        with :func:`is_known_payer` when the caller wants to say so.

    Raises:
        ValueError: The name holds no alphanumeric characters, so there is
            nothing to normalise. An empty payer is a caller bug, unlike an
            unfamiliar one.
    """
    slug = slugify_payer(raw)
    if not slug:
        raise ValueError("Payer name contains no alphanumeric characters.")
    return PAYER_ALIASES.get(slug, slug)


def is_known_payer(slug: str) -> bool:
    """Report whether a canonical slug is one the vocabulary knows about.

    False is not a failure and must not be turned into one. It means the caller
    should log at WARNING — see this module's docstring — so that a payer name
    which never lined up with anything we ingested is visible in the operational
    trace instead of looking like a payer with no policies.

    Args:
        slug: A slug from :func:`normalize_payer`, not a raw display name.
    """
    return slug in KNOWN_PAYERS

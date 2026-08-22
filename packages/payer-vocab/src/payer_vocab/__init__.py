"""One canonical vocabulary for payer names and jurisdiction state codes.

``payer`` is compared by exact string equality in two places that decide whether
a query finds anything: the Qdrant retrieval filter in ``track_b_rag.retrieval``
and the ``rag:{payer}:{plan_type}:{state}:{cpt_code}`` cache key. Until this
package existed, nothing normalised it on either side — ingestion stored whatever
the uploader sent and the query filtered on whatever the caller sent. At query
time that string comes from a FHIR ``Coverage`` resource's free-text display:
"Medicare Part B", "AETNA", "Aetna Better Health of MA". None of those equals the
"CMS" or "Aetna" an ingest wrote, so retrieval returned nothing and the service
reported that it found no policy — indistinguishable from a payer we genuinely
hold nothing for.

So: a payer is a slug everywhere it is stored, matched or keyed, and never a
display name. Both sides call the same function::

    from payer_vocab import is_known_payer, normalize_payer

    payer = normalize_payer(body.payer)      # "Medicare Part B" -> "cms-medicare"
    if not is_known_payer(payer):
        logger.warning("Query for unrecognised payer %r (from %r)", payer, body.payer)

Keep the payer's own spelling for humans to read — ``insurance_policies`` still
records it. Slugs are for matching, not for display.

:mod:`payer_vocab.states` handles the same problem for jurisdictions, where CMS's
Medicare Coverage Database uses sub-state codes (``DN``, ``QN``, ``UN``, ``NF``,
``SF``, ``EM``, ``WM``) and a four-character ``CNMI`` that no FHIR resource will
ever produce and that does not fit the ``CHAR(2)`` column besides.

See CLAUDE.md, "Payer and jurisdiction identity — one canonical vocabulary".
"""

from payer_vocab.payers import (
    KNOWN_PAYERS,
    PAYER_ALIASES,
    is_known_payer,
    normalize_payer,
    slugify_payer,
)
from payer_vocab.states import (
    CMS_JURISDICTION_CODES,
    USPS_STATE_CODES,
    normalize_state,
    normalize_states,
)

__all__ = [
    "CMS_JURISDICTION_CODES",
    "KNOWN_PAYERS",
    "PAYER_ALIASES",
    "USPS_STATE_CODES",
    "is_known_payer",
    "normalize_payer",
    "normalize_state",
    "normalize_states",
    "slugify_payer",
]

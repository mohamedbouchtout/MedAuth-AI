"""Which payers expose the Da Vinci interoperability APIs, as canonical slugs.

Two callers ask a question about the same underlying fact, from opposite ends of
the product:

* ``track_b_rag.crd`` asks it mid-encounter, to decide whether to consult a
  payer's own CRD endpoint alongside the RAG path (TASK-015).
* ``prior_auth`` asks it after the visit, to decide whether a submission has an
  automated path at all (TASK-061).

The fact is **CMS-0057-F**, the interoperability rule that obliges impacted
payers — Medicare Advantage, Medicaid managed care, CHIP and ACA marketplace
plans — to expose the Da Vinci APIs. A payer inside its scope is expected to
answer over CRD *and* to accept a PAS submission; a payer outside it is expected
to do neither. That is one predicate answering two questions, which is why one
set serves both rather than two sets that would have to be kept in step.

**If a payer is ever observed publishing one and not the other, that is the
moment to split this into two sets** — and the split will then record something
real that somebody saw, rather than a generality invented in advance. The same
discipline the alias table in :mod:`payer_vocab.payers` follows: extend from
observed data, never from plausible speculation.

This lives here rather than in either caller because the second consumer arrived
(TASK-061) and copying a literal set into a second private module is how two
spellings of one vocabulary begin. That is the extraction trigger already applied
to ``packages/api-envelope``, ``packages/session-auth`` and to this package
itself — and CLAUDE.md's standing preference for collapsing a duplication over
detecting its drift.

Everything here is a **canonical slug** from :func:`payer_vocab.normalize_payer`,
never a display name. A caller holding a payer's own spelling normalises first;
passing "Medicare Advantage" straight in silently answers ``False`` and looks
exactly like a payer outside the mandate, which is the failure this whole package
exists to prevent.
"""

from __future__ import annotations

from typing import Final

#: Payer slugs whose plans CMS-0057-F covers.
#:
#: Deliberately a literal set and not a configuration file: it is two entries
#: today, and it becomes real payer capability data — endpoints, registration,
#: per-plan variation — once payers actually publish it. Building a framework for
#: that now would be designing against a shape nobody has seen.
#:
#: The mandate also covers CHIP and ACA marketplace plans. Neither has a slug
#: yet, because no ``Coverage.payor.display`` observed so far has produced one;
#: adding speculative slugs here would be the "extend the alias table from
#: plausible spellings" mistake this package's own notes rule out. Add each when
#: a real payer name resolves to it.
CMS_0057_PAYERS: Final[frozenset[str]] = frozenset(
    {
        "medicare-advantage",
        "medicaid",
    }
)


def supports_crd(payer: str) -> bool:
    """Return whether this payer is expected to answer over Da Vinci CRD.

    Args:
        payer: The canonical payer slug. A display name will not match, which is
            the same silent failure the vocabulary exists to prevent — callers
            normalise before asking, not here, because a function that
            normalised its own argument would hide the one place a caller
            forgot to.

    Returns:
        True for a payer covered by the CMS-0057-F mandate.
    """
    return payer in CMS_0057_PAYERS


def supports_prior_auth_api(payer: str) -> bool:
    """Return whether this payer is expected to accept a Da Vinci PAS submission.

    The same predicate as :func:`supports_crd` today, under a name that says what
    the caller is actually asking. Two names rather than one alias because the
    day the two diverge, every call site already says which capability it meant —
    whereas a single shared function would have to be read and re-judged at each
    one.

    **False is not a failure.** Most commercial employer-sponsored plans sit
    outside the mandate, and a request for one is submitted by a human rather
    than not at all. The caller's job is to say so, never to report it as an
    error and never to leave it looking transmitted.

    Args:
        payer: The canonical payer slug, normalised by the caller.

    Returns:
        True for a payer covered by the CMS-0057-F mandate.
    """
    return payer in CMS_0057_PAYERS

"""Deciding which CMS documents this scrape collects, and where they apply.

Two joins, neither of them obvious from the outside.

**The code-to-document index lives in the Articles, not the LCDs.** CMS moved
procedure code lists out of LCDs into companion Billing & Coding Articles, so
``lcd_x_hcpc_code`` holds codes for only 66 LCDs — all durable medical equipment
— and essentially no physician CPT codes at all. Filtering it directly matches
almost nothing while looking exactly like a working scraper that found no work
to do. The join that works is ``article_x_hcpc_code`` → ``article_related_documents``
→ ``lcd_id``.

**An LCD's jurisdiction is a set of states, not one state.** It is issued by a
Medicare Administrative Contractor and applies across that contractor's whole
jurisdiction — a median of twelve states across the current export, up to
forty-eight. Resolving it means ``lcd_x_contractor`` → ``contractor_jurisdiction``
→ ``state_lookup``, and then normalising, because CMS's state vocabulary is not
USPS: it carries ``DN``/``QN``/``UN`` for parts of New York, ``NF``/``SF`` for
California, ``EM``/``WM`` for Missouri, and a four-character ``CNMI``. None of
those would ever match a state that arrived on a FHIR Coverage.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from payer_vocab import normalize_state

logger = logging.getLogger(__name__)


def lcd_ids_for_codes(
    *,
    article_codes: list[dict[str, str]],
    article_documents: list[dict[str, str]],
    target_codes: set[str],
) -> set[str]:
    """Return the LCD ids whose billing articles cover any of the target codes.

    Args:
        article_codes: ``article_x_hcpc_code`` rows.
        article_documents: ``article_related_documents`` rows.
        target_codes: The CPT/HCPCS codes to collect policies for.

    Returns:
        The distinct ``lcd_id`` values to fetch. Empty is a legitimate answer
        only if the target codes genuinely have no Medicare coverage documents —
        which is true of several of them — so the caller logs the count rather
        than treating zero as success or failure.
    """
    # Both sides are uppercased. The export's codes already are, but a target
    # code is written by hand, and one lowercase j-code would silently select
    # nothing while looking like a procedure Medicare does not cover.
    wanted = {code.strip().upper() for code in target_codes}
    articles = {
        row["article_id"] for row in article_codes if row["hcpc_code_id"].strip().upper() in wanted
    }

    related: dict[str, set[str]] = defaultdict(set)
    for row in article_documents:
        if row.get("r_lcd_id"):
            related[row["article_id"]].add(row["r_lcd_id"])

    lcd_ids = {lcd_id for article in articles for lcd_id in related.get(article, ())}
    logger.info(
        "Selected %s LCD(s) from %s article(s) matching %s target code(s)",
        len(lcd_ids),
        len(articles),
        len(target_codes),
    )
    return lcd_ids


def jurisdiction_states(
    *,
    lcd_contractors: list[dict[str, str]],
    contractor_jurisdictions: list[dict[str, str]],
    states: list[dict[str, str]],
) -> dict[str, list[str]]:
    """Return each LCD's jurisdiction as sorted USPS state codes.

    Contractor rows are keyed on the triple ``(contractor_id, contractor_type_id,
    contractor_version)`` — a contractor id alone is ambiguous, because the same
    organisation appears under several types and versions with different
    jurisdictions. Rows carrying a ``term_date`` are expired assignments and are
    skipped; including them would claim coverage in states a contractor no
    longer serves.

    Unrecognised state codes are dropped with a warning rather than failing the
    scrape: one unknown code should cost that state, not the policy.
    """
    abbreviations = {row["state_id"]: row["state_abbrev"] for row in states}

    by_contractor: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in contractor_jurisdictions:
        if row.get("term_date", "").strip():
            continue
        key = (row["contractor_id"], row["contractor_type_id"], row["contractor_version"])
        raw = abbreviations.get(row["state_id"])
        if raw is None:
            logger.warning("Jurisdiction row names unknown state_id %r", row["state_id"])
            continue
        try:
            by_contractor[key].add(normalize_state(raw))
        except ValueError:
            logger.warning("Skipping unrecognised CMS state code %r", raw)

    by_lcd: dict[str, set[str]] = defaultdict(set)
    for row in lcd_contractors:
        key = (row["contractor_id"], row["contractor_type_id"], row["contractor_version"])
        by_lcd[row["lcd_id"]] |= by_contractor.get(key, set())

    return {lcd_id: sorted(codes) for lcd_id, codes in by_lcd.items()}

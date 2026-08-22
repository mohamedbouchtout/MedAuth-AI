"""Assembling one policy document out of the export's fields.

A policy is spread across several columns of a single CSV row — its coverage
criteria in one, its evidence summary in another — each holding an HTML
fragment. The document this service uploads is those fragments concatenated in a
fixed order, which is why the order is a module constant rather than a
dictionary iteration: **the digest is taken over exactly these bytes**, so a
change in field order would look like every policy changing at once.

For the same reason the row's own metadata — ``last_updated``, ``lcd_version`` —
is not part of the document. Those describe the export, not the policy, and
folding them in would re-ingest a document whose text never moved. What CMS
publishes is what gets hashed; nothing here reformats, re-indents or re-encodes
the fragments on the way through.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)

#: The payer every CMS document is ingested under, already canonical. Not
#: "CMS" — see payer_vocab for why a display name here matches nothing.
CMS_PAYER: Final = "cms-medicare"

#: LCD body fields, in the order they are concatenated. Titles aside, this is
#: roughly the order the rendered page presents them.
LCD_FIELDS: Final[tuple[str, ...]] = (
    "cms_cov_policy",
    "issue",
    "indication",
    "associated_info",
    "summary_of_evidence",
    "analysis_of_evidence",
    "bibliography",
)

#: NCD body fields. A different table with different column names, so the two
#: cannot share one list.
NCD_FIELDS: Final[tuple[str, ...]] = ("itm_srvc_desc", "indctn_lmtn", "xref_txt", "othr_txt")


@dataclass(frozen=True)
class PolicyDocument:
    """One CMS policy, ready to upload."""

    policy_id: str
    title: str
    body: bytes
    #: Sorted USPS codes for a local coverage determination; empty for a
    #: national one, which applies everywhere and is ingested with no state.
    states: list[str]
    source_url: str
    effective_date: datetime.date | None

    @property
    def content_hash(self) -> str:
        """SHA-256 over the bytes uploaded, matching what ingest will compute.

        This is what the pre-upload skip compares against
        ``insurance_policies.content_hash``. It is an optimisation only: ingest
        recomputes the digest from the bytes it receives and its answer is the
        authoritative one.
        """
        return hashlib.sha256(self.body).hexdigest()


def _assemble(title: str, row: dict[str, str], fields: tuple[str, ...]) -> bytes | None:
    """Return the document body, or None when the row carries no policy text.

    A title is not policy text. A row with a heading and nothing else would
    otherwise assemble into a document, chunk into one heading, and record a
    content hash against a vector that says nothing — which every later scrape
    of those same bytes then reports as "unchanged". So the emptiness check
    looks at the body fields rather than at the assembled bytes.
    """
    populated = [row[field] for field in fields if row.get(field, "").strip()]
    if not populated:
        return None
    parts = [f"<h1>{title}</h1>"] if title.strip() else []
    parts.extend(populated)
    return "\n".join(parts).encode("utf-8")


def _parse_date(value: str) -> datetime.date | None:
    """Return the date in a CMS timestamp column, or None if it is absent.

    The export writes dates as ``YYYY-MM-DD HH:MM:SS``. An unparseable value is
    worth a log line and a null column, not a failed scrape — the policy text is
    what matters and the date is provenance.
    """
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        logger.warning("Could not parse effective date %r; recording none", value)
        return None


def build_lcd(
    row: dict[str, str], *, states: list[str], coverage_db_base_url: str
) -> PolicyDocument | None:
    """Return the uploadable document for one ``lcd.csv`` row, or None if empty.

    A row whose body fields are all blank has nothing to index. Uploading it
    would store a content hash with no vectors behind it — the one state
    TASK-011's dedup cannot recover from, since every later scrape of those same
    bytes then reports "unchanged".
    """
    body = _assemble(row.get("title", ""), row, LCD_FIELDS)
    lcd_id = row["lcd_id"]
    if body is None:
        logger.warning("LCD %s carries no policy text in the export; skipping", lcd_id)
        return None

    return PolicyDocument(
        # display_id is the L-number a human recognises ("L39529"); lcd_id is
        # CMS's internal key. Falling back keeps the identifier stable either way.
        policy_id=f"cms-lcd-{row.get('display_id') or f'L{lcd_id}'}",
        title=row.get("title", ""),
        body=body,
        states=states,
        source_url=f"{coverage_db_base_url}/view/lcd.aspx?lcdid={lcd_id}",
        effective_date=_parse_date(row.get("rev_eff_date") or row.get("orig_det_eff_date", "")),
    )


def build_ncd(row: dict[str, str], *, coverage_db_base_url: str) -> PolicyDocument | None:
    """Return the uploadable document for one ``ncd_trkg.csv`` row, or None.

    National coverage determinations carry no state: they apply everywhere. That
    is stored as no state at all rather than as fifty-odd of them, which is what
    lets the retrieval filter's null branch return them alongside whichever
    local policy a query's state matched.
    """
    body = _assemble(row.get("NCD_mnl_sect_title", ""), row, NCD_FIELDS)
    ncd_id = row["NCD_id"]
    if body is None:
        logger.warning("NCD %s carries no policy text in the export; skipping", ncd_id)
        return None

    return PolicyDocument(
        policy_id=f"cms-ncd-{ncd_id}",
        title=row.get("NCD_mnl_sect_title", ""),
        body=body,
        states=[],
        source_url=f"{coverage_db_base_url}/view/ncd.aspx?ncdid={ncd_id}",
        effective_date=_parse_date(row.get("NCD_efctv_dt", "")),
    )

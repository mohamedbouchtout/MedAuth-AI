"""One nightly scrape, start to finish.

The shape of a run:

1. Fetch three archives from CMS — the NCD export, the LCD export and the
   article export. That is every request this service makes. The exports carry
   the full policy text, so there is nothing to crawl per document.
2. Work out which LCDs matter, by joining the target procedure codes through the
   Billing & Coding articles that carry them, and resolve each one's contractor
   jurisdiction to USPS state codes.
3. Assemble each document out of the export's fields, in a fixed order, and
   digest it.
4. Skip whatever the database already has under that digest, and upload the
   rest to ``/policies/ingest``.

The summary at the end is the operational record. Nothing here writes an audit
row: coverage determinations are public payer publications with no patient
linkage, so Known Constraints #6 says this service logs at INFO and leaves the
audit table to routes that actually touch PHI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from policy_scraper import mcd, selection
from policy_scraper.codes import TARGET_CODES
from policy_scraper.config import Settings
from policy_scraper.documents import PolicyDocument, build_lcd, build_ncd
from policy_scraper.fetch import PoliteClient
from policy_scraper.ingest import upload
from policy_scraper.store import known_content_hashes, session_factory

logger = logging.getLogger(__name__)

#: The columns each export table has to keep for this service to work. Passed to
#: the reader so a renamed column fails loudly during the nightly live check
#: rather than reading as every row being empty.
LCD_TABLES = {
    "lcd": [
        "lcd_id",
        "display_id",
        "title",
        "cms_cov_policy",
        "issue",
        "indication",
        "associated_info",
        "summary_of_evidence",
        "analysis_of_evidence",
        "bibliography",
        "rev_eff_date",
        "orig_det_eff_date",
    ],
    "lcd_x_contractor": ["lcd_id", "contractor_id", "contractor_type_id", "contractor_version"],
    "contractor_jurisdiction": [
        "contractor_id",
        "contractor_type_id",
        "contractor_version",
        "state_id",
        "term_date",
    ],
    "state_lookup": ["state_id", "state_abbrev"],
}

ARTICLE_TABLES = {
    "article_x_hcpc_code": ["article_id", "hcpc_code_id"],
    "article_related_documents": ["article_id", "r_lcd_id"],
}

NCD_TABLES = {
    "ncd_trkg": [
        "NCD_id",
        "NCD_mnl_sect_title",
        "itm_srvc_desc",
        "indctn_lmtn",
        "xref_txt",
        "othr_txt",
        "NCD_efctv_dt",
    ],
}


@dataclass
class ScrapeSummary:
    """What one run did, for the log line and for the tests."""

    considered: int = 0
    skipped_unchanged: int = 0
    uploaded: int = 0
    failed: int = 0
    statuses: dict[str, int] = field(default_factory=dict)

    def record(self, status: str) -> None:
        """Count one ingest result by the status the endpoint reported."""
        self.uploaded += 1
        self.statuses[status] = self.statuses.get(status, 0) + 1


async def collect_documents(settings: Settings, client: PoliteClient) -> list[PolicyDocument]:
    """Fetch the exports and return the documents this run should consider.

    National coverage determinations are all collected: there are 357 of them,
    they apply everywhere, and CMS's export gives them no procedure-code index
    to filter on. Local coverage determinations are filtered to the target
    codes, which is what keeps the run from indexing durable medical equipment
    policies a specialty practice will never order against.
    """
    exports = settings.cms_mcd_exports_base_url

    article_tables = mcd.read_tables(
        await client.get(f"{exports}/{mcd.ARTICLE_EXPORT}"), ARTICLE_TABLES
    )
    lcd_tables = mcd.read_tables(await client.get(f"{exports}/{mcd.LCD_EXPORT}"), LCD_TABLES)
    ncd_tables = mcd.read_tables(await client.get(f"{exports}/{mcd.NCD_EXPORT}"), NCD_TABLES)

    wanted = selection.lcd_ids_for_codes(
        article_codes=article_tables["article_x_hcpc_code"],
        article_documents=article_tables["article_related_documents"],
        target_codes=set(TARGET_CODES),
    )
    states = selection.jurisdiction_states(
        lcd_contractors=lcd_tables["lcd_x_contractor"],
        contractor_jurisdictions=lcd_tables["contractor_jurisdiction"],
        states=lcd_tables["state_lookup"],
    )

    documents: list[PolicyDocument] = []
    for row in lcd_tables["lcd"]:
        if row["lcd_id"] not in wanted:
            continue
        document = build_lcd(
            row,
            states=states.get(row["lcd_id"], []),
            coverage_db_base_url=settings.cms_coverage_db_base_url,
        )
        if document is not None:
            documents.append(document)

    for row in ncd_tables["ncd_trkg"]:
        document = build_ncd(row, coverage_db_base_url=settings.cms_coverage_db_base_url)
        if document is not None:
            documents.append(document)

    logger.info("Assembled %s document(s) to consider", len(documents))
    return documents


async def upload_documents(
    documents: list[PolicyDocument],
    *,
    settings: Settings,
    ingest_client: httpx.AsyncClient,
) -> ScrapeSummary:
    """Upload everything whose digest the database does not already hold."""
    summary = ScrapeSummary(considered=len(documents))

    factory = session_factory(settings.database_url)
    async with factory() as session:
        stored = await known_content_hashes(session, {doc.policy_id for doc in documents})

    for document in documents:
        if stored.get(document.policy_id) == document.content_hash:
            summary.skipped_unchanged += 1
            continue
        try:
            result = await upload(
                ingest_client, base_url=settings.track_b_rag_url, document=document
            )
        except (httpx.HTTPError, ValueError, RuntimeError):
            # One document failing is not the run failing. The count is in the
            # summary and the traceback is in the log, so a policy that cannot
            # be ingested is visible without costing the other forty.
            logger.exception("Failed to ingest %s", document.policy_id)
            summary.failed += 1
            continue
        summary.record(str(result["status"]))

    return summary


async def run(settings: Settings) -> ScrapeSummary:
    """Run one scrape and return what it did."""
    async with PoliteClient(
        user_agent=settings.policy_scraper_user_agent,
        delay_seconds=settings.request_delay_seconds,
        timeout_seconds=settings.download_timeout_seconds,
    ) as client:
        documents = await collect_documents(settings, client)

    async with httpx.AsyncClient(timeout=settings.ingest_timeout_seconds) as ingest_client:
        summary = await upload_documents(documents, settings=settings, ingest_client=ingest_client)

    logger.info(
        "Scrape complete: %s considered, %s unchanged, %s uploaded (%s), %s failed",
        summary.considered,
        summary.skipped_unchanged,
        summary.uploaded,
        summary.statuses,
        summary.failed,
    )
    return summary

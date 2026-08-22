"""The live check against CMS, gated and scheduled.

**Why this is gated.** It fetches ~75 MB from a government service. Running it
on every pull request would make an unrelated change go red because CMS was
slow, and would hammer a public server for no reason.

**Why gating it is not the same as deleting it.** A gate nobody ever opens means
CMS's export layout could change and nobody would find out until someone
happened to set the flag — which is no better than loosening the assertions
until they stop failing. So `.github/workflows/nightly-live-checks.yml` runs
this on a schedule with `RUN_CMS_LIVE_TESTS=1`, and a red nightly naming CMS is
the signal that the scraper needs updating.

**Do not relax anything here to make a failure go away.** Every assertion below
is a structural claim the scraper depends on. If one breaks, the scraper is
already broken and this is how we find out.
"""

from __future__ import annotations

import os

import pytest

from policy_scraper import mcd, scrape, selection
from policy_scraper.codes import TARGET_CODES
from policy_scraper.config import Settings
from policy_scraper.documents import build_lcd
from policy_scraper.fetch import PoliteClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_CMS_LIVE_TESTS") != "1",
        reason="live CMS check; set RUN_CMS_LIVE_TESTS=1 (the nightly workflow does)",
    ),
]

SETTINGS = Settings(
    policy_scraper_user_agent=os.environ.get(
        "POLICY_SCRAPER_USER_AGENT",
        "MedAuthAI-PolicyScraper/0.1 (+https://medauth.ai; scraper@medauth.ai)",
    ),
    database_url="postgresql+asyncpg://unused/unused",
)


@pytest.fixture(scope="module")
async def exports() -> dict[str, bytes]:
    """Download the three archives once for the whole module.

    Session-scoped downloads of ~75 MB, so every assertion below shares them.
    The delay between requests is the configured one; this is a real client
    against a real government service.
    """
    async with PoliteClient(
        user_agent=SETTINGS.policy_scraper_user_agent,
        delay_seconds=SETTINGS.request_delay_seconds,
        timeout_seconds=SETTINGS.download_timeout_seconds,
    ) as client:
        base = SETTINGS.cms_mcd_exports_base_url
        return {
            name: await client.get(f"{base}/{name}")
            for name in (mcd.NCD_EXPORT, mcd.LCD_EXPORT, mcd.ARTICLE_EXPORT)
        }


async def test_every_export_is_still_published(exports: dict[str, bytes]) -> None:
    """The three archives the whole service is built on."""
    assert all(len(payload) > 100_000 for payload in exports.values())


async def test_the_lcd_export_still_has_the_tables_and_columns_we_read(
    exports: dict[str, bytes],
) -> None:
    """A renamed column would otherwise read as every row being empty, which
    looks like a policy with no text rather than like breakage."""
    tables = mcd.read_tables(exports[mcd.LCD_EXPORT], scrape.LCD_TABLES)

    assert tables["lcd"]
    assert tables["state_lookup"]


async def test_the_article_export_still_has_the_code_index(
    exports: dict[str, bytes],
) -> None:
    """The join that finds a policy for a CPT code. CMS moved codes out of LCDs
    into these articles once already; if it moves them again, this is where we
    find out."""
    tables = mcd.read_tables(exports[mcd.ARTICLE_EXPORT], scrape.ARTICLE_TABLES)

    assert tables["article_x_hcpc_code"]


async def test_the_ncd_export_still_has_its_tracking_table(
    exports: dict[str, bytes],
) -> None:
    tables = mcd.read_tables(exports[mcd.NCD_EXPORT], scrape.NCD_TABLES)

    assert tables["ncd_trkg"]


async def test_the_target_codes_still_reach_coverage_documents(
    exports: dict[str, bytes],
) -> None:
    """The scrape's whole point. Zero here means either CMS restructured the
    article join or every one of our codes lost its coverage document — both
    need a human, and neither should pass silently."""
    articles = mcd.read_tables(exports[mcd.ARTICLE_EXPORT], scrape.ARTICLE_TABLES)

    selected = selection.lcd_ids_for_codes(
        article_codes=articles["article_x_hcpc_code"],
        article_documents=articles["article_related_documents"],
        target_codes=set(TARGET_CODES),
    )

    assert selected, "no LCD matched any target code"


async def test_a_real_policy_downloads_and_hashes(exports: dict[str, bytes]) -> None:
    """TASK-013's acceptance test: at least one policy downloaded and hashed."""
    tables = mcd.read_tables(exports[mcd.LCD_EXPORT], scrape.LCD_TABLES)
    articles = mcd.read_tables(exports[mcd.ARTICLE_EXPORT], scrape.ARTICLE_TABLES)

    selected = selection.lcd_ids_for_codes(
        article_codes=articles["article_x_hcpc_code"],
        article_documents=articles["article_related_documents"],
        target_codes=set(TARGET_CODES),
    )
    states = selection.jurisdiction_states(
        lcd_contractors=tables["lcd_x_contractor"],
        contractor_jurisdictions=tables["contractor_jurisdiction"],
        states=tables["state_lookup"],
    )

    documents = [
        document
        for row in tables["lcd"]
        if row["lcd_id"] in selected
        and (
            document := build_lcd(
                row,
                states=states.get(row["lcd_id"], []),
                coverage_db_base_url=SETTINGS.cms_coverage_db_base_url,
            )
        )
        is not None
    ]

    assert documents, "no policy could be assembled from the export"
    assert all(len(document.content_hash) == 64 for document in documents)
    assert any(document.states for document in documents), (
        "no selected LCD resolved to any state — the contractor jurisdiction "
        "join has probably changed"
    )


async def test_a_real_policy_carries_real_criteria_text(exports: dict[str, bytes]) -> None:
    """Guards against the export going structurally fine but semantically empty
    — tables present, columns present, policy bodies blank."""
    tables = mcd.read_tables(exports[mcd.LCD_EXPORT], scrape.LCD_TABLES)

    bodies = [row["indication"] for row in tables["lcd"] if row["indication"].strip()]

    assert len(bodies) > 100, "almost no LCD carries indication text any more"

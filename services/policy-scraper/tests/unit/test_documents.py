"""Assembling a policy document, and the digest taken over it.

The digest is what decides whether a nightly run re-embeds a policy, so the
claims about determinism here are the ones the whole schedule rests on.
"""

from __future__ import annotations

import datetime

from policy_scraper.documents import build_lcd, build_ncd
from tests.fixtures import LCD_ROWS, NCD_ROWS

BASE_URL = "https://www.cms.gov/medicare-coverage-database"


def lcd(index: int = 0, states: list[str] | None = None) -> object:
    return build_lcd(LCD_ROWS[index], states=states or ["MA"], coverage_db_base_url=BASE_URL)


class TestLcdDocuments:
    def test_the_policy_id_uses_the_l_number_a_human_recognises(self) -> None:
        assert lcd().policy_id == "cms-lcd-L39529"  # type: ignore[union-attr]

    def test_the_body_carries_the_coverage_criteria(self) -> None:
        assert b"six weeks" in lcd().body  # type: ignore[union-attr]

    def test_the_body_carries_the_title(self) -> None:
        assert b"Intraarticular Knee Injections" in lcd().body  # type: ignore[union-attr]

    def test_blank_fields_are_omitted_rather_than_left_as_gaps(self) -> None:
        """L34220's evidence sections are empty in the export; the document
        should not carry the blank lines they would leave."""
        body = lcd(1).body.decode()  # type: ignore[union-attr]

        assert "\n\n" not in body

    def test_the_jurisdiction_is_carried_through(self) -> None:
        assert lcd(states=["MA", "NY"]).states == ["MA", "NY"]  # type: ignore[union-attr]

    def test_the_source_url_points_at_the_document_a_human_would_read(self) -> None:
        assert lcd().source_url.endswith("/view/lcd.aspx?lcdid=39529")  # type: ignore[union-attr]

    def test_the_effective_date_prefers_the_revision(self) -> None:
        """A revised policy took effect when the revision did, not when the
        original determination did."""
        assert lcd().effective_date == datetime.date(2025, 5, 1)  # type: ignore[union-attr]

    def test_the_original_date_is_used_when_there_is_no_revision(self) -> None:
        assert lcd(1).effective_date == datetime.date(2015, 10, 1)  # type: ignore[union-attr]

    def test_an_unparseable_date_is_recorded_as_none(self) -> None:
        """Provenance is worth a log line, not a failed scrape."""
        row = {**LCD_ROWS[0], "rev_eff_date": "not a date", "orig_det_eff_date": ""}

        assert build_lcd(row, states=[], coverage_db_base_url=BASE_URL).effective_date is None  # type: ignore[union-attr]

    def test_a_row_with_no_dates_at_all_records_none(self) -> None:
        """Most NCDs and some LCDs carry neither date; that is not a warning."""
        row = {**LCD_ROWS[0], "rev_eff_date": "", "orig_det_eff_date": ""}

        assert build_lcd(row, states=[], coverage_db_base_url=BASE_URL).effective_date is None  # type: ignore[union-attr]

    def test_a_row_with_no_text_yields_no_document(self) -> None:
        """Uploading it would record a content hash with no vectors behind it —
        the one state TASK-011's dedup cannot recover from, since every later
        scrape of those same bytes then reports "unchanged"."""
        keep = ("lcd_id", "display_id")
        empty = {key: (value if key in keep else "") for key, value in LCD_ROWS[0].items()}

        assert build_lcd(empty, states=[], coverage_db_base_url=BASE_URL) is None


class TestNcdDocuments:
    def test_a_national_determination_carries_no_state(self) -> None:
        """Null is what the retrieval filter's IsNullCondition looks for, and it
        is how an NCD reaches a query from any state."""
        assert build_ncd(NCD_ROWS[0], coverage_db_base_url=BASE_URL).states == []  # type: ignore[union-attr]

    def test_the_policy_id_names_the_manual_section(self) -> None:
        assert build_ncd(NCD_ROWS[0], coverage_db_base_url=BASE_URL).policy_id == "cms-ncd-220.2"  # type: ignore[union-attr]

    def test_the_body_carries_the_indications(self) -> None:
        body = build_ncd(NCD_ROWS[0], coverage_db_base_url=BASE_URL).body  # type: ignore[union-attr]

        assert b"Nationally covered" in body

    def test_an_empty_determination_yields_no_document(self) -> None:
        assert build_ncd(NCD_ROWS[1], coverage_db_base_url=BASE_URL) is None


class TestContentHash:
    def test_the_same_row_digests_the_same_way_every_run(self) -> None:
        """The property the rejected render-to-PDF approach lacked. Without it
        every nightly scrape reads as an update and re-embeds the corpus."""
        assert lcd().content_hash == lcd().content_hash  # type: ignore[union-attr]

    def test_changed_policy_text_changes_the_digest(self) -> None:
        revised = {**LCD_ROWS[0], "indication": "<p>Revised criteria.</p>"}

        assert (
            build_lcd(revised, states=["MA"], coverage_db_base_url=BASE_URL).content_hash  # type: ignore[union-attr]
            != lcd().content_hash  # type: ignore[union-attr]
        )

    def test_export_metadata_does_not_reach_the_digest(self) -> None:
        """last_updated and lcd_version describe the export, not the policy.
        Folding them in would re-ingest a document whose text never moved."""
        restamped = {**LCD_ROWS[0], "last_updated": "2026-08-22 03:30:00", "lcd_version": "99"}

        assert (
            build_lcd(restamped, states=["MA"], coverage_db_base_url=BASE_URL).content_hash  # type: ignore[union-attr]
            == lcd().content_hash  # type: ignore[union-attr]
        )

    def test_the_jurisdiction_does_not_reach_the_digest(self) -> None:
        """The digest identifies the document. A contractor gaining a state is a
        metadata change, and re-embedding the text for it would be waste."""
        assert lcd(states=["MA"]).content_hash == lcd(states=["MA", "NY"]).content_hash  # type: ignore[union-attr]

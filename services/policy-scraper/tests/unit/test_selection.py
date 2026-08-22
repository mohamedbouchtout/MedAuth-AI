"""Which documents a run collects, and where each one applies.

Both joins here are the kind that fail silently when they are wrong: a scrape
that selects nothing looks exactly like a scrape with nothing to do, and a
jurisdiction resolved to codes no FHIR Coverage will ever produce looks exactly
like a payer with no policies on file.
"""

from __future__ import annotations

import pytest

from policy_scraper.selection import jurisdiction_states, lcd_ids_for_codes
from tests.fixtures import (
    ARTICLE_CODE_ROWS,
    ARTICLE_DOCUMENT_ROWS,
    CONTRACTOR_JURISDICTION_ROWS,
    LCD_CONTRACTOR_ROWS,
    STATE_ROWS,
)


def select(codes: set[str]) -> set[str]:
    return lcd_ids_for_codes(
        article_codes=ARTICLE_CODE_ROWS,
        article_documents=ARTICLE_DOCUMENT_ROWS,
        target_codes=codes,
    )


class TestSelectingLcds:
    def test_a_target_code_reaches_its_lcd_through_the_article(self) -> None:
        """The join CMS's structure forces: codes live in Billing & Coding
        articles, not in the LCDs, so filtering lcd_x_hcpc_code directly would
        match almost nothing while looking like a working scraper."""
        assert select({"J7325"}) == {"39529"}

    def test_two_codes_in_one_article_select_it_once(self) -> None:
        assert select({"J7325", "20610"}) == {"39529"}

    def test_codes_in_different_articles_select_both(self) -> None:
        assert select({"J7325", "72148"}) == {"39529", "34220"}

    def test_an_untargeted_code_selects_nothing(self) -> None:
        """The wheelchair LCD is in the fixture precisely so the filter has
        something it must leave alone."""
        assert "33312" not in select({"J7325", "72148"})

    def test_a_code_with_no_coverage_document_selects_nothing(self) -> None:
        """29881 is the real case: knee arthroscopy has no LCD and no article.
        An empty result is the honest answer, not a bug to work around."""
        assert select({"29881"}) == set()

    def test_matching_is_case_insensitive(self) -> None:
        """Codes arrive uppercased elsewhere in the system; a lowercase j-code
        in the export should not silently miss."""
        assert select({"j7325"}) == select({"J7325"})

    def test_an_article_related_to_no_lcd_is_skipped(self) -> None:
        """article_related_documents also links articles to other articles, and
        those rows carry an empty r_lcd_id."""
        assert "" not in select({"J7325", "20610"})


class TestJurisdictions:
    @pytest.fixture
    def states(self) -> dict[str, list[str]]:
        return jurisdiction_states(
            lcd_contractors=LCD_CONTRACTOR_ROWS,
            contractor_jurisdictions=CONTRACTOR_JURISDICTION_ROWS,
            states=STATE_ROWS,
        )

    def test_an_lcd_covers_every_state_in_its_contractors_jurisdiction(
        self, states: dict[str, list[str]]
    ) -> None:
        assert states["39529"] == ["MA", "NY"]

    def test_sub_state_codes_collapse_to_their_state(self, states: dict[str, list[str]]) -> None:
        """DN and UN are both New York. The list is of states, not of CMS's
        jurisdictions, because a state is what arrives on a Coverage resource."""
        assert states["39529"].count("NY") == 1

    def test_a_four_character_code_is_normalised(self, states: dict[str, list[str]]) -> None:
        """CNMI does not even fit the CHAR(2) column it would be written to."""
        assert "MP" in states["34220"]

    def test_northern_california_is_california(self, states: dict[str, list[str]]) -> None:
        assert "CA" in states["34220"]

    def test_an_expired_assignment_is_ignored(self, states: dict[str, list[str]]) -> None:
        """A contractor that no longer serves Texas does not cover Texas, and
        claiming otherwise would answer a Texas query out of the wrong policy."""
        assert "TX" not in states["39529"]

    def test_the_list_is_sorted_for_stability(self, states: dict[str, list[str]]) -> None:
        """A jurisdiction that has not changed must produce the same list every
        night, or the row churns for no reason."""
        assert states["39529"] == sorted(states["39529"])

    def test_a_contractor_with_no_jurisdiction_rows_yields_no_states(
        self, states: dict[str, list[str]]
    ) -> None:
        assert states["33312"] == []

    def test_an_unrecognised_state_code_is_dropped_not_fatal(self) -> None:
        """One unknown code should cost that state, not the whole policy."""
        result = jurisdiction_states(
            lcd_contractors=LCD_CONTRACTOR_ROWS,
            contractor_jurisdictions=CONTRACTOR_JURISDICTION_ROWS,
            states=[*STATE_ROWS, {"state_id": "24", "state_abbrev": "ZZ", "description": "?"}],
        )

        assert "ZZ" not in result["39529"]

    def test_a_jurisdiction_row_naming_an_unknown_state_id_is_skipped(self) -> None:
        result = jurisdiction_states(
            lcd_contractors=LCD_CONTRACTOR_ROWS,
            contractor_jurisdictions=[
                *CONTRACTOR_JURISDICTION_ROWS,
                {
                    "contractor_id": "143",
                    "contractor_type_id": "1",
                    "contractor_version": "1",
                    "state_id": "9999",
                    "term_date": "",
                },
            ],
            states=STATE_ROWS,
        )

        assert result["39529"] == ["MA", "NY"]

    def test_contractors_are_keyed_on_the_whole_triple(self) -> None:
        """The same contractor id appears under several types and versions with
        different jurisdictions, so an id alone would pull in the wrong states."""
        result = jurisdiction_states(
            lcd_contractors=[
                {
                    "lcd_id": "1",
                    "contractor_id": "143",
                    "contractor_type_id": "99",
                    "contractor_version": "1",
                }
            ],
            contractor_jurisdictions=CONTRACTOR_JURISDICTION_ROWS,
            states=STATE_ROWS,
        )

        assert result["1"] == []

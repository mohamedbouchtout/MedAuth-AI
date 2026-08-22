"""Reading the export archives, and noticing when CMS changes their shape.

The column check is the point of this module. A renamed column would otherwise
read as every row having an empty value, which looks like a policy with no text
rather than like breakage — and a scrape that indexes nothing while reporting
success is the failure this service is written to avoid.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from policy_scraper.mcd import ExportFormatError, read_tables
from tests.fixtures import article_export, lcd_export, ncd_export

LCD_COLUMNS = {"lcd": ["lcd_id", "title", "indication"]}


class TestReadingTables:
    def test_a_table_comes_back_as_rows(self) -> None:
        tables = read_tables(lcd_export(), LCD_COLUMNS)

        assert len(tables["lcd"]) == 3
        assert tables["lcd"][0]["display_id"] == "L39529"

    def test_several_tables_are_read_from_one_archive(self) -> None:
        tables = read_tables(
            lcd_export(),
            {"lcd": ["lcd_id"], "state_lookup": ["state_id", "state_abbrev"]},
        )

        assert set(tables) == {"lcd", "state_lookup"}

    def test_the_nested_archive_is_found_by_name(self) -> None:
        """The outer zip also holds an Access database and a PDF dictionary."""
        assert read_tables(ncd_export(), {"ncd_trkg": ["NCD_id"]})["ncd_trkg"]

    def test_policy_text_longer_than_the_default_field_limit_is_read(self) -> None:
        """Real indication fields run to tens of kilobytes, past csv's default."""
        big = "<p>" + ("criteria " * 40_000) + "</p>"
        rows = [{"lcd_id": "1", "title": "t", "indication": big}]
        export = _one_table_export("lcd", rows)

        assert len(read_tables(export, LCD_COLUMNS)["lcd"][0]["indication"]) == len(big)

    def test_an_empty_cell_reads_as_an_empty_string_not_none(self) -> None:
        """Downstream code calls .strip() on these without checking for None."""
        tables = read_tables(article_export(), {"article_related_documents": ["r_lcd_id"]})

        assert all(row["r_lcd_id"] is not None for row in tables["article_related_documents"])


class TestFormatDrift:
    def test_a_missing_table_is_an_error(self) -> None:
        with pytest.raises(ExportFormatError, match="not in the export"):
            read_tables(lcd_export(), {"lcd_x_hcpc_code": ["lcd_id"]})

    def test_a_missing_column_is_an_error(self) -> None:
        """Not a warning and not an empty result: this is the nightly check's
        whole purpose, and a column CMS renamed needs a human."""
        with pytest.raises(ExportFormatError, match="missing column"):
            read_tables(lcd_export(), {"lcd": ["lcd_id", "coverage_criteria"]})

    def test_the_error_names_the_columns_that_are_there(self) -> None:
        """So whoever reads the failure can see what it was renamed to."""
        with pytest.raises(ExportFormatError, match="indication"):
            read_tables(lcd_export(), {"lcd": ["nonexistent_column"]})

    def test_an_archive_with_no_csv_zip_inside_is_an_error(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("current_lcd.mdb", b"not a csv archive")

        with pytest.raises(ExportFormatError, match="No \\*_csv.zip"):
            read_tables(buffer.getvalue(), LCD_COLUMNS)


def _one_table_export(name: str, rows: list[dict[str, str]]) -> bytes:
    from tests.fixtures import build_export

    return build_export({name: rows}, inner_name="current_lcd_csv.zip")

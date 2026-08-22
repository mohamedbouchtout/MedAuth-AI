"""Reading CMS's Medicare Coverage Database exports.

CMS regenerates three archives daily at about 02:00 UTC:
``ncd.zip`` (~1 MB), ``current_lcd.zip`` (~32 MB) and ``current_article.zip``
(~41 MB). Each is a zip holding a second zip of CSV tables, plus an Access
database and a data dictionary this service ignores.

**Three archive fetches, and no per-document requests.** The export carries the
full policy body — ``lcd.csv`` in ``indication``, ``summary_of_evidence`` and
their neighbours, ``ncd_trkg.csv`` in ``itm_srvc_desc`` and ``indctn_lmtn`` — so
there is nothing to crawl. The rendered pages on ``www.cms.gov`` add AMA and AHA
licence boilerplate and navigation chrome that this pipeline is better off
without, and they are not byte-stable besides: each response carries a
per-request CSP nonce, so a digest taken over one would change on every fetch
and mark every policy updated every night.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from typing import Final

logger = logging.getLogger(__name__)

#: The archives this service reads. The NCD export is separate from the LCD one
#: and has an entirely different table layout, hence two paths through
#: `documents.py` rather than one generic reader.
NCD_EXPORT: Final = "ncd.zip"
LCD_EXPORT: Final = "current_lcd.zip"
ARTICLE_EXPORT: Final = "current_article.zip"

#: Some policy text fields run past the default 128 KB field limit.
_FIELD_LIMIT: Final = 10 * 1024 * 1024


class ExportFormatError(RuntimeError):
    """Raised when an export does not hold the tables or columns we read.

    This is the failure the nightly live check exists to surface. CMS changing
    its export layout is a real event that needs a human, and it must not be
    caught and turned into "no policies found today".
    """


def _inner_csv_archive(archive: zipfile.ZipFile) -> zipfile.ZipFile:
    """Return the nested zip of CSV tables inside an export archive."""
    names = [name for name in archive.namelist() if name.lower().endswith("_csv.zip")]
    if not names:
        raise ExportFormatError(
            f"No *_csv.zip inside the export; found {archive.namelist()!r}. "
            "CMS may have changed the export layout."
        )
    return zipfile.ZipFile(io.BytesIO(archive.read(names[0])))


def read_tables(
    export_bytes: bytes, tables: dict[str, list[str]]
) -> dict[str, list[dict[str, str]]]:
    """Return the named CSV tables from an export archive.

    Args:
        export_bytes: One downloaded export archive.
        tables: Table name (without ``.csv``) to the columns this service reads
            from it. The columns are checked, not just the table: a renamed
            column would otherwise read as every row having an empty value,
            which looks like a policy with no text rather than like breakage.

    Returns:
        Each requested table as a list of row dicts.

    Raises:
        ExportFormatError: A table or a column this service depends on is gone.
    """
    previous_limit = csv.field_size_limit(_FIELD_LIMIT)
    try:
        with zipfile.ZipFile(io.BytesIO(export_bytes)) as outer:
            with _inner_csv_archive(outer) as inner:
                available = {name.lower(): name for name in inner.namelist()}
                result: dict[str, list[dict[str, str]]] = {}
                for table, required in tables.items():
                    filename = available.get(f"{table}.csv")
                    if filename is None:
                        raise ExportFormatError(
                            f"Table {table}.csv is not in the export. "
                            "CMS may have renamed or dropped it."
                        )
                    rows = _read_table(inner, filename, table, required)
                    result[table] = rows
                    logger.info("Read %s rows from %s", len(rows), filename)
                return result
    finally:
        csv.field_size_limit(previous_limit)


def _read_table(
    archive: zipfile.ZipFile, filename: str, table: str, required: list[str]
) -> list[dict[str, str]]:
    """Return one table's rows, after checking the columns we read still exist."""
    with archive.open(filename) as handle:
        text = io.TextIOWrapper(handle, encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.DictReader(text)
        columns = set(reader.fieldnames or ())
        missing = [column for column in required if column not in columns]
        if missing:
            raise ExportFormatError(
                f"Table {table}.csv is missing column(s) {missing!r}. "
                f"It has {sorted(columns)!r}. CMS may have changed the schema."
            )
        return [{key: (value or "") for key, value in row.items() if key} for row in reader]

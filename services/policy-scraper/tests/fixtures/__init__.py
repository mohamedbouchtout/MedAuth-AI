"""Recorded CMS export data, shrunk to what the tests need.

The rows and column names are copied from a real Medicare Coverage Database
export rather than invented, so a test that passes here is testing against the
shape CMS actually publishes. The nightly live check (``RUN_CMS_LIVE_TESTS=1``)
is what notices when that shape changes; these fixtures are what let the rest of
the suite run on a laptop with no network.
"""

from __future__ import annotations

import csv
import io
import zipfile

# --- lcd.csv ---------------------------------------------------------------
# L39529 "Intraarticular Knee Injections of Hyaluronan" and L34220 "Lumbar MRI"
# are real documents; the bodies are cut to a sentence each.
LCD_ROWS = [
    {
        "lcd_id": "39529",
        "display_id": "L39529",
        "title": "Intraarticular Knee Injections of Hyaluronan",
        "cms_cov_policy": "<p>This LCD supplements but does not replace existing policy.</p>",
        "issue": "<p>This LCD outlines limited coverage for this service.</p>",
        "indication": (
            "<p>Compliance with the provisions in this policy may be monitored.</p>"
            "<p>Conservative therapy is defined as: nonpharmacologic therapy such as "
            "home exercise, and a documented failure of six weeks of it.</p>"
        ),
        "associated_info": "<p>Refer to the Billing and Coding article.</p>",
        "summary_of_evidence": "<p>Various polymers of hyaluronic acid are marketed.</p>",
        "analysis_of_evidence": "<p>The evidence is of moderate quality.</p>",
        "bibliography": "<ol><li>American Academy of Orthopaedic Surgeons.</li></ol>",
        "rev_eff_date": "2025-05-01 00:00:00",
        "orig_det_eff_date": "2023-06-11 00:00:00",
    },
    {
        "lcd_id": "34220",
        "display_id": "L34220",
        "title": "Lumbar MRI",
        "cms_cov_policy": "",
        "issue": "",
        "indication": "<p>Magnetic resonance imaging of the lumbar spine is covered when.</p>",
        "associated_info": "",
        "summary_of_evidence": "",
        "analysis_of_evidence": "",
        "bibliography": "",
        "rev_eff_date": "",
        "orig_det_eff_date": "2015-10-01 00:00:00",
    },
    {
        # A policy no target code reaches: the filter has to leave it alone.
        "lcd_id": "33312",
        "display_id": "L33312",
        "title": "Wheelchair Options and Accessories",
        "cms_cov_policy": "",
        "issue": "",
        "indication": "<p>Wheelchair accessories are covered when.</p>",
        "associated_info": "",
        "summary_of_evidence": "",
        "analysis_of_evidence": "",
        "bibliography": "",
        "rev_eff_date": "2024-01-01 00:00:00",
        "orig_det_eff_date": "2014-01-01 00:00:00",
    },
]

# --- the article join ------------------------------------------------------
# CMS moved procedure codes out of LCDs into Billing & Coding articles, so this
# is the join that actually finds a policy for a CPT code.
ARTICLE_CODE_ROWS = [
    {"article_id": "58754", "hcpc_code_id": "J7325"},
    {"article_id": "58754", "hcpc_code_id": "20610"},
    {"article_id": "52458", "hcpc_code_id": "72148"},
    {"article_id": "99999", "hcpc_code_id": "E0953"},  # wheelchair accessory
]

ARTICLE_DOCUMENT_ROWS = [
    {"article_id": "58754", "r_lcd_id": "39529"},
    {"article_id": "52458", "r_lcd_id": "34220"},
    {"article_id": "99999", "r_lcd_id": "33312"},
    # An article related to another article rather than an LCD: r_lcd_id empty.
    {"article_id": "58754", "r_lcd_id": ""},
]

# --- contractor jurisdictions ----------------------------------------------
# 143 is a WPS jurisdiction spanning several states; 297 is a Novitas one. The
# expired row is what `term_date` filtering exists for.
LCD_CONTRACTOR_ROWS = [
    {
        "lcd_id": "39529",
        "contractor_id": "143",
        "contractor_type_id": "1",
        "contractor_version": "1",
    },
    {
        "lcd_id": "34220",
        "contractor_id": "297",
        "contractor_type_id": "1",
        "contractor_version": "1",
    },
    {
        "lcd_id": "33312",
        "contractor_id": "139",
        "contractor_type_id": "10",
        "contractor_version": "2",
    },
]

CONTRACTOR_JURISDICTION_ROWS = [
    {
        "contractor_id": "143",
        "contractor_type_id": "1",
        "contractor_version": "1",
        "state_id": "24",
        "term_date": "",
    },  # MA
    {
        "contractor_id": "143",
        "contractor_type_id": "1",
        "contractor_version": "1",
        "state_id": "63",
        "term_date": "",
    },  # DN -> NY
    {
        "contractor_id": "143",
        "contractor_type_id": "1",
        "contractor_version": "1",
        "state_id": "65",
        "term_date": "",
    },  # UN -> NY
    {
        "contractor_id": "143",
        "contractor_type_id": "1",
        "contractor_version": "1",
        "state_id": "51",
        "term_date": "2019-12-31 00:00:00",
    },  # TX, expired
    {
        "contractor_id": "297",
        "contractor_type_id": "1",
        "contractor_version": "1",
        "state_id": "66",
        "term_date": "",
    },  # NF -> CA
    {
        "contractor_id": "297",
        "contractor_type_id": "1",
        "contractor_version": "1",
        "state_id": "60",
        "term_date": "",
    },  # CNMI -> MP
]

#: The subset of CMS's state_lookup the fixtures use — including the codes that
#: are not USPS codes, which is the point.
STATE_ROWS = [
    {"state_id": "24", "state_abbrev": "MA", "description": "Massachusetts"},
    {"state_id": "51", "state_abbrev": "TX", "description": "Texas"},
    {"state_id": "60", "state_abbrev": "CNMI", "description": "Northern Mariana Islands"},
    {"state_id": "63", "state_abbrev": "DN", "description": "New York - Downstate"},
    {"state_id": "65", "state_abbrev": "UN", "description": "New York - Upstate"},
    {"state_id": "66", "state_abbrev": "NF", "description": "California - Northern"},
]

# --- ncd_trkg.csv ----------------------------------------------------------
NCD_ROWS = [
    {
        "NCD_id": "220.2",
        "NCD_mnl_sect_title": "Magnetic Resonance Imaging",
        "itm_srvc_desc": "<p>MRI is a non-invasive imaging method.</p>",
        "indctn_lmtn": "<p>Nationally covered when the criteria below are met.</p>",
        "xref_txt": "",
        "othr_txt": "",
        "NCD_efctv_dt": "2007-07-07 00:00:00",
    },
    {
        # Nothing to index. Uploading it would store a hash with no vectors.
        "NCD_id": "999.9",
        "NCD_mnl_sect_title": "Reserved",
        "itm_srvc_desc": "",
        "indctn_lmtn": "",
        "xref_txt": "",
        "othr_txt": "",
        "NCD_efctv_dt": "",
    },
]


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    """Render rows as a CSV the reader can parse."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def build_export(tables: dict[str, list[dict[str, str]]], *, inner_name: str) -> bytes:
    """Return a zip-inside-a-zip of CSV tables, the way CMS ships one.

    The nesting is not incidental — the outer archive holds an Access database
    and a PDF data dictionary alongside the CSV archive, and the reader has to
    find the inner one by name.
    """
    inner_buffer = io.BytesIO()
    with zipfile.ZipFile(inner_buffer, "w") as inner:
        for name, rows in tables.items():
            inner.writestr(f"{name}.csv", _csv_bytes(rows))
        inner.writestr("readme_first.txt", b"To view the downloadable database...")

    outer_buffer = io.BytesIO()
    with zipfile.ZipFile(outer_buffer, "w") as outer:
        outer.writestr(inner_name, inner_buffer.getvalue())
        outer.writestr("lcd data dictionary.pdf", b"%PDF-1.4 not read by anything")
    return outer_buffer.getvalue()


def lcd_export() -> bytes:
    """The LCD export archive."""
    return build_export(
        {
            "lcd": LCD_ROWS,
            "lcd_x_contractor": LCD_CONTRACTOR_ROWS,
            "contractor_jurisdiction": CONTRACTOR_JURISDICTION_ROWS,
            "state_lookup": STATE_ROWS,
        },
        inner_name="current_lcd_csv.zip",
    )


def article_export() -> bytes:
    """The article export archive."""
    return build_export(
        {
            "article_x_hcpc_code": ARTICLE_CODE_ROWS,
            "article_related_documents": ARTICLE_DOCUMENT_ROWS,
        },
        inner_name="current_article_csv.zip",
    )


def ncd_export() -> bytes:
    """The NCD export archive."""
    return build_export({"ncd_trkg": NCD_ROWS}, inner_name="ncd_csv.zip")

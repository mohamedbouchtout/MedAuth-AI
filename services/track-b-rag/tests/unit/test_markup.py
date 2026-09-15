"""Getting text out of the HTML that CMS publishes instead of PDFs.

The inputs to build for are fragments — the contents of a policy's coverage
section as the Medicare Coverage Database export carries it — not whole pages.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from track_b_rag import markup
from track_b_rag.documents import DocumentParseError
from track_b_rag.markup import HtmlParseError, extract_text

REPO_ROOT = Path(__file__).resolve().parents[4]

#: Two real consecutive fetches of Aetna CPB 1009, captured for TASK-009. They
#: live with the package that defines what is stripped; any change under
#: packages/ re-tests every service, so editing them re-runs this file too.
AETNA_FIXTURES = REPO_ROOT / "packages" / "html-digest" / "tests" / "fixtures" / "aetna"
AETNA_FIRST_FETCH = AETNA_FIXTURES / "cpb-1009-first-fetch.html"
AETNA_SECOND_FETCH = AETNA_FIXTURES / "cpb-1009-second-fetch.html"


def test_a_fragment_needs_no_surrounding_document() -> None:
    """What the export carries is the section body, with no html or head around it."""
    text = extract_text(b"<p>Compliance with this policy may be monitored.</p>")

    assert text == "Compliance with this policy may be monitored."


def test_tags_are_removed_and_their_text_kept() -> None:
    text = extract_text(b"<p>Failure of <strong>six weeks</strong> of therapy.</p>")

    assert text == "Failure of six weeks of therapy."


def test_entities_are_decoded() -> None:
    text = extract_text(b"<p>Conservative&nbsp;therapy &amp; imaging &lt;see below&gt;</p>")

    assert "&amp;" not in text
    assert "&" in text
    assert "<see below>" in text


def test_block_elements_become_blank_lines() -> None:
    """The chunker splits on paragraph boundaries first, so this is what lets a
    criteria list break between criteria rather than mid-sentence."""
    text = extract_text(b"<p>First criterion.</p><p>Second criterion.</p>")

    assert text == "First criterion.\n\nSecond criterion."


def test_list_items_are_separate_blocks() -> None:
    text = extract_text(b"<ol><li>Conservative therapy.</li><li>Imaging.</li></ol>")

    assert text == "Conservative therapy.\n\nImaging."


def test_table_cells_are_separate_blocks() -> None:
    text = extract_text(b"<table><tr><td>72148</td><td>Lumbar MRI</td></tr></table>")

    assert text == "72148\n\nLumbar MRI"


def test_nesting_does_not_multiply_blank_lines() -> None:
    """Empty blocks drop out, so a run of nested elements is one boundary."""
    text = extract_text(b"<div><ul><li><p>Only item.</p></li></ul></div><p>After.</p>")

    assert text == "Only item.\n\nAfter."


def test_source_formatting_whitespace_is_collapsed() -> None:
    """Newlines and indentation in the markup are formatting, not sentence structure."""
    text = extract_text(b"<p>Failure of\n    six weeks\n    of therapy.</p>")

    assert text == "Failure of six weeks of therapy."


def test_script_and_style_contents_are_dropped() -> None:
    """A fragment lifted from a rendered page can carry them, and script source
    read as prose would be chunked and embedded like any other sentence."""
    text = extract_text(
        b"<style>p { color: red }</style><p>Real criterion.</p>"
        b"<script>var lcdLink = '/view/lcd.aspx';</script>"
    )

    assert text == "Real criterion."


def test_extraction_discards_what_the_digest_ignores_using_the_same_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One definition of script and style, not two matchers (TASK-009): the
    bytes the parser sees are the ones html_digest's scanner left."""
    seen: list[bytes] = []

    def recording_strip(data: bytes) -> bytes:
        seen.append(data)
        return data.replace(b"<script>x()</script>", b"")

    monkeypatch.setattr(markup, "strip_non_text", recording_strip)

    assert extract_text(b"<p>A</p><script>x()</script>") == "A"
    assert seen == [b"<p>A</p><script>x()</script>"]


def test_the_parser_guard_suppresses_and_warns_if_the_scan_misses_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If the stdlib ever finds a script the shared scanner did not, its source
    must still stay out of the index — and the disagreement must be visible."""
    monkeypatch.setattr(markup, "strip_non_text", lambda data: data)

    with caplog.at_level(logging.WARNING, logger="track_b_rag.markup"):
        text = extract_text(b"<p>Criterion.</p><script>var lcdLink = 1;</script>")

    assert text == "Criterion."
    assert "survived html_digest's scan" in caplog.text


def test_two_real_aetna_fetches_extract_to_the_same_text() -> None:
    assert extract_text(AETNA_FIRST_FETCH.read_bytes()) == extract_text(
        AETNA_SECOND_FETCH.read_bytes()
    )


def test_stripping_first_extracts_what_the_parser_alone_extracted_from_a_real_page() -> None:
    """Moving script and style removal to the shared scanner changed nothing the
    index holds: the parser's own suppression, run on the unstripped page, gives
    the same text."""
    page = AETNA_FIRST_FETCH.read_bytes()
    parser_alone = markup._TextExtractor()
    parser_alone.feed(page.decode("utf-8"))
    parser_alone.close()

    assert extract_text(page) == parser_alone.text()


def test_the_scanner_and_the_parser_agree_on_a_real_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Eight elements, a commented-out script and four noscript blocks, and not
    one reaches the parser's guard — on whichever Python runs this."""
    with caplog.at_level(logging.WARNING, logger="track_b_rag.markup"):
        text = extract_text(AETNA_FIRST_FETCH.read_bytes())

    assert caplog.records == []
    assert "go-mpulse" not in text
    assert "Risankizumab" in text


def test_a_stray_closing_tag_does_not_suppress_the_rest() -> None:
    """Fragments are not guaranteed balanced, and swallowing the document after
    one unmatched tag would lose a policy silently."""
    text = extract_text(b"</script><p>Criterion after a stray tag.</p>")

    assert text == "Criterion after a stray tag."


def test_markup_with_no_text_extracts_to_nothing() -> None:
    """Ingestion rejects this the same way it rejects a PDF with no text layer."""
    assert extract_text(b"<div><br/></div>") == ""


def test_cp1252_bytes_are_decoded_rather_than_rejected() -> None:
    """Payer documents carry smart quotes, and a strict-UTF-8-only reader would
    reject a policy over its apostrophes."""
    text = extract_text("<p>the payer’s criteria</p>".encode("cp1252"))

    assert "payer" in text and "criteria" in text


#: Invalid as UTF-8 (0xFF starts no sequence) and invalid as cp1252 (0x81 is one
#: of the five undefined bytes in that codepage), so it defeats both attempts.
#: Most non-UTF-8 payer documents *are* cp1252, which is why the fallback exists
#: and why this input has to be chosen deliberately to get past it.
UNDECODABLE = b"\xff\x81"


def test_undecodable_bytes_raise_a_parse_error() -> None:
    with pytest.raises(HtmlParseError):
        extract_text(UNDECODABLE)


def test_the_parse_error_is_a_document_parse_error() -> None:
    """The route catches the base class, so both formats answer 400 alike."""
    with pytest.raises(DocumentParseError):
        extract_text(UNDECODABLE)


def test_the_parse_error_names_no_file_content() -> None:
    with pytest.raises(HtmlParseError) as caught:
        extract_text(UNDECODABLE + b"secret-looking bytes")

    assert "secret-looking" not in str(caught.value)

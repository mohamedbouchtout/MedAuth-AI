"""Getting text out of the HTML that CMS publishes instead of PDFs.

The inputs to build for are fragments — the contents of a policy's coverage
section as the Medicare Coverage Database export carries it — not whole pages.
"""

from __future__ import annotations

import pytest

from track_b_rag.documents import DocumentParseError
from track_b_rag.markup import HtmlParseError, extract_text


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

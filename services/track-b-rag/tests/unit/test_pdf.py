"""Digest and text extraction — the two things a policy PDF is read for."""

from __future__ import annotations

import hashlib

import pymupdf
import pytest

from track_b_rag.pdf import PdfParseError, content_digest, extract_text


def build_pdf(pages: list[str]) -> bytes:
    """Return a real PDF carrying one text block per page."""
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text)
    data: bytes = document.tobytes()
    document.close()
    return data


def test_the_digest_is_sha256_over_the_raw_bytes() -> None:
    pdf = build_pdf(["Prior authorization is required."])

    assert content_digest(pdf) == hashlib.sha256(pdf).hexdigest()


def test_the_same_bytes_digest_the_same_way() -> None:
    pdf = build_pdf(["Coverage criteria."])

    assert content_digest(pdf) == content_digest(pdf)


def test_different_bytes_digest_differently() -> None:
    assert content_digest(b"%PDF-1.4 one") != content_digest(b"%PDF-1.4 two")


def test_the_digest_covers_the_file_not_the_text() -> None:
    """Two PDFs with identical text are still distinct source files (TASK-011)."""
    first = build_pdf(["Identical policy text."])
    second = build_pdf(["Identical policy text.", ""])

    assert extract_text(first) == extract_text(second)
    assert content_digest(first) != content_digest(second)


def test_extracted_text_carries_every_page() -> None:
    pdf = build_pdf(["First page criteria.", "Second page criteria."])

    text = extract_text(pdf)

    assert "First page criteria." in text
    assert "Second page criteria." in text


def test_pages_are_separated_by_a_blank_line() -> None:
    pdf = build_pdf(["Alpha", "Beta"])

    assert "\n\n" in extract_text(pdf)


def test_a_pdf_with_no_text_layer_extracts_to_nothing() -> None:
    """A scanned document parses cleanly and yields nothing — ingestion rejects it."""
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    document.new_page()
    empty = document.tobytes()
    document.close()

    assert extract_text(empty).strip() == ""


def test_bytes_that_are_not_a_pdf_raise_a_parse_error() -> None:
    with pytest.raises(PdfParseError):
        extract_text(b"this is not a pdf at all")


def test_a_truncated_pdf_raises_a_parse_error() -> None:
    pdf = build_pdf(["Some policy text."])

    with pytest.raises(PdfParseError):
        extract_text(pdf[: len(pdf) // 4])


def test_the_parse_error_names_no_file_content() -> None:
    """The message is about the upload, not about what was in it."""
    with pytest.raises(PdfParseError) as caught:
        extract_text(b"secret-looking bytes")

    assert "secret-looking" not in str(caught.value)

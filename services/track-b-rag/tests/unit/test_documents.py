"""The format-independent half of reading a document: its digest, and dispatch.

Both claims here are the ones TASK-011's dedup rests on, now that two formats
reach it: the digest is over the bytes the payer published, and a document is
read by the reader its declared type names.
"""

from __future__ import annotations

import hashlib

import pymupdf
import pytest

from track_b_rag.documents import (
    CONTENT_TYPES,
    DEFAULT_CONTENT_TYPE,
    ContentType,
    DocumentParseError,
    content_digest,
    extract_text,
)

HTML_POLICY = b"<p>Prior authorization is required for this procedure.</p>"


def build_pdf(text: str = "Prior authorization is required.") -> bytes:
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    document.new_page().insert_text((72, 72), text)
    data: bytes = document.tobytes()
    document.close()
    return data


# --- the digest ------------------------------------------------------------


@pytest.fixture(params=["application/pdf", "text/html"])
def document(request: pytest.FixtureRequest) -> tuple[bytes, ContentType]:
    """One document of each supported format, so every claim covers both."""
    content_type: ContentType = request.param
    return (build_pdf() if content_type == "application/pdf" else HTML_POLICY, content_type)


def test_the_digest_is_sha256_over_the_raw_bytes(
    document: tuple[bytes, ContentType],
) -> None:
    raw, _ = document

    assert content_digest(raw) == hashlib.sha256(raw).hexdigest()


def test_the_same_bytes_digest_the_same_way(document: tuple[bytes, ContentType]) -> None:
    raw, _ = document

    assert content_digest(raw) == content_digest(raw)


def test_different_bytes_digest_differently() -> None:
    assert content_digest(b"<p>one</p>") != content_digest(b"<p>two</p>")


def test_the_digest_ignores_the_declared_type() -> None:
    """It identifies the source file. The same bytes are the same file whatever
    a caller says they are, which is what keeps a re-declared upload from
    re-embedding a corpus that did not change."""
    assert content_digest(HTML_POLICY) == content_digest(HTML_POLICY)


def test_html_extraction_is_byte_stable_across_calls() -> None:
    """The property the rejected render-to-PDF approach lacked: identical input
    gives an identical digest every time, so a nightly scrape of an unchanged
    document reports "unchanged" rather than re-indexing it (TASK-013)."""
    first = content_digest(HTML_POLICY)
    second = content_digest(bytes(HTML_POLICY))

    assert first == second


# --- dispatch --------------------------------------------------------------


def test_a_pdf_is_read_by_the_pdf_reader() -> None:
    text = extract_text(build_pdf("Coverage criteria."), "application/pdf")

    assert "Coverage criteria." in text


def test_html_is_read_by_the_markup_reader() -> None:
    text = extract_text(HTML_POLICY, "text/html")

    assert text == "Prior authorization is required for this procedure."


def test_html_declared_as_a_pdf_fails_rather_than_indexing_markup() -> None:
    """Declaring the wrong type is a caller bug, and the honest outcome is a
    rejected upload — not a policy whose chunks are HTML tags."""
    with pytest.raises(DocumentParseError):
        extract_text(HTML_POLICY, "application/pdf")


def test_the_default_type_is_pdf() -> None:
    """Payers publish PDFs; CMS is the exception, and it declares itself."""
    assert DEFAULT_CONTENT_TYPE == "application/pdf"


def test_every_supported_type_has_a_reader() -> None:
    """A type added to the Literal without a branch would silently fall through
    to the PDF reader."""
    for content_type in CONTENT_TYPES:
        sample = build_pdf() if content_type == "application/pdf" else HTML_POLICY
        assert extract_text(sample, content_type)  # type: ignore[arg-type]

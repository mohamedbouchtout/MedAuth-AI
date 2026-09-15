"""The format-independent half of reading a document: its digest, and dispatch.

Both claims here are the ones TASK-011's dedup rests on, now that two formats
reach it: the digest is over the bytes the payer published — for HTML, less its
script and style elements (TASK-009) — and a document is read by the reader its
declared type names.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

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


#: Two real consecutive fetches of Aetna CPB 1009, captured for TASK-009. They
#: live with the package that defines what is stripped; any change under
#: packages/ re-tests every service, so editing them re-runs this file too.
AETNA_FIXTURES = (
    Path(__file__).resolve().parents[4] / "packages" / "html-digest" / "tests" / "fixtures"
) / "aetna"


def test_a_pdf_digests_to_the_sha256_of_its_raw_bytes() -> None:
    raw = build_pdf()

    assert content_digest(raw, "application/pdf") == hashlib.sha256(raw).hexdigest()


def test_a_pdf_is_never_stripped() -> None:
    """Only HTML is scanned. Bytes that look like a script inside a PDF are the
    PDF's own, and a digest that skipped them could merge two distinct files."""
    raw = build_pdf() + b"<script>x()</script>"

    assert content_digest(raw, "application/pdf") == hashlib.sha256(raw).hexdigest()


def test_script_free_html_digests_to_the_sha256_of_its_raw_bytes() -> None:
    """Every HTML digest stored before TASK-009 was taken over bytes like these,
    so not one of them moves."""
    assert content_digest(HTML_POLICY, "text/html") == hashlib.sha256(HTML_POLICY).hexdigest()


def test_html_differing_only_inside_a_script_is_one_document() -> None:
    first = HTML_POLICY + b'<script>token="a1b2"</script>'
    second = HTML_POLICY + b'<script>token="c3d4"</script>'

    assert content_digest(first, "text/html") == content_digest(second, "text/html")


def test_html_differing_in_policy_prose_is_two_documents() -> None:
    """The fix narrows the digest; it must not make it blind to the policy."""
    first = b"<p>Six weeks of therapy.</p><script>t=1</script>"
    second = b"<p>Twelve weeks of therapy.</p><script>t=1</script>"

    assert content_digest(first, "text/html") != content_digest(second, "text/html")


def test_the_same_bytes_digest_the_same_way(document: tuple[bytes, ContentType]) -> None:
    raw, content_type = document

    assert content_digest(raw, content_type) == content_digest(bytes(raw), content_type)


def test_script_free_bytes_digest_the_same_whichever_type_is_declared() -> None:
    """What the old "the digest ignores the declared type" claim still holds
    for: with nothing to strip, a re-declared upload does not re-embed."""
    assert content_digest(HTML_POLICY, "text/html") == content_digest(
        HTML_POLICY, "application/pdf"
    )


def test_two_real_fetches_of_an_aetna_page_are_one_document() -> None:
    """TASK-009's cause, on the capture: the raw bytes differ, the digest does not."""
    first = (AETNA_FIXTURES / "cpb-1009-first-fetch.html").read_bytes()
    second = (AETNA_FIXTURES / "cpb-1009-second-fetch.html").read_bytes()

    assert hashlib.sha256(first).digest() != hashlib.sha256(second).digest()
    assert content_digest(first, "text/html") == content_digest(second, "text/html")


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

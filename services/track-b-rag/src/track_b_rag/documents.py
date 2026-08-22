"""What a policy document is, regardless of the format it arrived in.

The ingest pipeline needs two things from an upload: a digest that identifies
the source file, and its text. Both are format-independent questions, so they
live here and the per-format readers — :mod:`track_b_rag.pdf` and
:mod:`track_b_rag.markup` — answer only the second one.

**Two formats, because the sources publish two.** Commercial payers publish
policy PDFs. CMS publishes its Medicare Coverage Database as HTML and offers no
PDF at all, so TASK-013's scraper has HTML to hand and nothing else. The
alternative — rendering that HTML to a PDF so this pipeline could stay
PDF-only — was rejected on measurement: PyMuPDF's output is not
byte-deterministic, so the same document rendered on two nights produces two
digests, every nightly scrape reads as an update, and the entire corpus is
re-embedded daily. That is exactly the cost ``content_hash`` exists to avoid.

The digest is still taken over the raw uploaded bytes, never over the extracted
text (TASK-011). Two documents whose text happens to match but whose bytes
differ are distinct source files for audit purposes.
"""

from __future__ import annotations

import hashlib
from typing import Final, Literal, get_args

#: The content types ``POST /policies/ingest`` accepts. Declared by the caller
#: rather than sniffed: a caller always knows what it fetched, and guessing
#: turns a wrong guess into a silently mis-parsed policy.
ContentType = Literal["application/pdf", "text/html"]

#: The same values as a tuple, for validation and for tests that assert every
#: supported type is actually handled.
CONTENT_TYPES: Final[tuple[str, ...]] = get_args(ContentType)

DEFAULT_CONTENT_TYPE: Final[ContentType] = "application/pdf"


class DocumentParseError(ValueError):
    """Raised when an uploaded document cannot be read in its declared format.

    The per-format errors derive from this so the ingest route can answer 400 to
    any of them without knowing which formats exist.
    """


def content_digest(raw_bytes: bytes) -> str:
    """Return the SHA-256 hex digest of the raw uploaded bytes.

    This is the value stored as ``insurance_policies.content_hash`` and the one
    the scraper (TASK-013) compares against to decide whether to re-ingest. It
    is deliberately over the bytes the payer published rather than over anything
    this service derives from them — a digest of our own rendering of a document
    changes when our rendering does, which would re-embed a corpus that never
    changed.
    """
    return hashlib.sha256(raw_bytes).hexdigest()


def extract_text(raw_bytes: bytes, content_type: ContentType) -> str:
    """Return the document's text, using the reader for its declared format.

    Args:
        raw_bytes: The uploaded document.
        content_type: What the caller says it is.

    Returns:
        The extracted text, with block structure preserved as blank lines so the
        chunker can split on a paragraph boundary. Both readers produce the same
        shape, which is why one chunking configuration serves both.

    Raises:
        DocumentParseError: The bytes are not readable in the declared format.
    """
    # Imported here rather than at module scope: pdf imports this module for
    # DocumentParseError, and markup does the same.
    from track_b_rag import markup, pdf

    if content_type == "text/html":
        return markup.extract_text(raw_bytes)
    return pdf.extract_text(raw_bytes)

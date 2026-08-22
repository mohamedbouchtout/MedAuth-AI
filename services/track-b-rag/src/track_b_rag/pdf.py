"""Reading a policy PDF: getting its text out.

One of the two document readers :mod:`track_b_rag.documents` dispatches to; the
other is :mod:`track_b_rag.markup`, for the HTML that CMS publishes instead of
PDFs. The digest that identifies a source file lives in ``documents`` with the
dispatcher, because it is the same question whatever the format.

Imported as ``pymupdf`` rather than the ``fitz`` name TASKS.md uses. They are the
same library — ``fitz`` is the legacy alias — but only the ``pymupdf`` module
ships a ``py.typed`` marker, so importing it keeps this module inside mypy strict
instead of needing an ``ignore_missing_imports`` override for the whole package.
"""

from __future__ import annotations

import logging

import pymupdf

from track_b_rag.documents import DocumentParseError

logger = logging.getLogger(__name__)

#: Separator between page texts. Blank-line separated so the chunker's paragraph
#: boundary is also a page boundary, rather than a sentence running across pages.
PAGE_SEPARATOR = "\n\n"


class PdfParseError(DocumentParseError):
    """Raised when the uploaded bytes are not a readable PDF."""


def extract_text(pdf_bytes: bytes) -> str:
    """Return the document's text, one blank-line-separated block per page.

    ``sort=True`` orders each page's text blocks by position rather than by the
    order they happen to appear in the content stream. Medical policy documents
    are frequently two-column, and without sorting the extracted text interleaves
    the columns line by line — which chunks into fluent-looking nonsense that no
    retrieval failure would obviously point back to here.

    Raises:
        PdfParseError: The bytes are not a PDF, or are damaged beyond reading.
    """
    try:
        # pymupdf ships py.typed but leaves Document's own constructor unannotated,
        # so strict mode rejects the call rather than the module. Ignored here at
        # the one call site instead of widening ignore_missing_imports for the
        # whole package, which would drop type checking on everything else it
        # exposes.
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:  # type: ignore[no-untyped-call]
            pages = [page.get_text("text", sort=True) for page in document]
    except Exception as exc:  # pymupdf raises several unrelated types here
        raise PdfParseError("The uploaded file could not be read as a PDF.") from exc

    return PAGE_SEPARATOR.join(page for page in pages if page.strip())

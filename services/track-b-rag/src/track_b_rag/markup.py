"""Reading an HTML policy document: its text, without the markup.

Payers publish policies as PDFs; CMS does not. Its Medicare Coverage Database
publishes LCDs and NCDs as HTML — the "PDF" affordance on the site is the
browser's own print-to-PDF — and the bulk export carries the same document body
as HTML fragments inside CSV fields. TASK-013's scraper ingests those fragments
directly, which is why this module exists alongside :mod:`track_b_rag.pdf`.

Fragments, not whole pages, is the case to build for: what arrives is the
contents of a policy's "Coverage Indications, Limitations, and/or Medical
Necessity" section, not a document with ``<html>`` and ``<head>`` around it.
Anything requiring a well-formed page would reject the input this is written to
read.

Extraction is stdlib :mod:`html.parser` rather than a parsing library. The job
is to get prose out of ``<p>``, ``<ul>`` and ``<table>`` markup with its
structure turned into blank lines, and adding BeautifulSoup to this service to
do that would be a dependency the work does not need.
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from typing import Final

from track_b_rag.documents import DocumentParseError

logger = logging.getLogger(__name__)


class HtmlParseError(DocumentParseError):
    """Raised when the uploaded bytes are not readable as HTML text."""


#: Elements whose boundaries are paragraph boundaries in the extracted text. The
#: chunker splits on blank lines first, so a list item or a table row that ends
#: here is a place it can split cleanly rather than mid-criterion.
_BLOCK_ELEMENTS: Final = frozenset(
    (
        "address article blockquote br caption div dd dl dt "
        "h1 h2 h3 h4 h5 h6 hr li ol p pre section table td th tr ul"
    ).split()
)

#: Elements whose *contents* are not document text. A policy fragment rarely
#: carries either, but a fragment lifted from a rendered page can, and script
#: source read as prose would be chunked and embedded like any other sentence.
_NON_TEXT_ELEMENTS: Final = frozenset({"script", "style"})

_BLOCK_SEPARATOR: Final = "\n\n"


class _TextExtractor(HTMLParser):
    """Collects an element's text, with block boundaries kept as blank lines."""

    def __init__(self) -> None:
        # convert_charrefs is the default and does the entity handling for us:
        # "&amp;" and "&nbsp;" arrive at handle_data already decoded.
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _NON_TEXT_ELEMENTS:
            self._suppressed += 1
        elif tag in _BLOCK_ELEMENTS:
            self._parts.append(_BLOCK_SEPARATOR)

    def handle_endtag(self, tag: str) -> None:
        if tag in _NON_TEXT_ELEMENTS:
            # Clamped at zero: a stray closing tag in a fragment must not leave
            # the extractor suppressing everything that follows it.
            self._suppressed = max(0, self._suppressed - 1)
        elif tag in _BLOCK_ELEMENTS:
            self._parts.append(_BLOCK_SEPARATOR)

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self._parts.append(data)

    def text(self) -> str:
        """Return the collected text, with runs of whitespace normalised."""
        blocks = ("".join(self._parts)).split(_BLOCK_SEPARATOR)
        # Each block collapses internally — source newlines and indentation are
        # markup formatting, not sentence structure — and empty blocks drop out,
        # so nested elements do not multiply into runs of blank lines.
        collapsed = [" ".join(block.split()) for block in blocks]
        return _BLOCK_SEPARATOR.join(block for block in collapsed if block)


def extract_text(html_bytes: bytes) -> str:
    """Return the document's text, one blank-line-separated block per element.

    Args:
        html_bytes: The document as published, which may be a fragment.

    Returns:
        The text with markup removed and block structure preserved as blank
        lines. Returns the empty string for markup that holds no text, which the
        caller rejects the same way it rejects a PDF with no text layer.

    Raises:
        HtmlParseError: The bytes are not decodable as text.
    """
    try:
        # errors="strict" on UTF-8 first: a mis-decoded policy is worse than a
        # rejected one, because it embeds cleanly and fails only at retrieval.
        markup = html_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            markup = html_bytes.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise HtmlParseError(
                "The uploaded file could not be decoded as UTF-8 or Windows-1252 text."
            ) from exc
        logger.info("Policy document decoded as cp1252 after UTF-8 failed")

    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    return extractor.text()

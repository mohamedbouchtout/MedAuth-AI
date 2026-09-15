"""Finding script and style elements as byte ranges, and cutting them out.

**Why a scanner of our own rather than** :mod:`html.parser`. A digest has to be
stable for as long as the published document is, and the stdlib tokenizer's
handling of script content has changed between Python patch releases — CI runs
3.12 while development runs 3.13. A digest that moved because the interpreter
did would re-embed a corpus that never changed, which is the failure this
package exists to end. So the rules are written down here, once, and pinned by
tests.

**Why bytes rather than decoded text.** Every character that matters to these
rules — ``<``, ``>``, ``!``, ``-``, ``/``, ``=``, quotes, whitespace and ASCII
letters — is ASCII. In UTF-8 an ASCII byte never occurs inside a multi-byte
sequence, and cp1252 (the other encoding :mod:`track_b_rag.markup` reads) is
one byte per character with ASCII at the same values. Scanning the raw bytes
therefore finds exactly the elements a scan of the decoded text would, needs no
decoding at all, and yields offsets into the bytes that are actually hashed.

**What it recognises, following the HTML tokenizer.** A start tag is ``<``
followed by an ASCII letter; its name runs to whitespace, ``/`` or ``>``, and is
compared case-insensitively. Attribute values in quotes may contain ``>``. The
content of a script or style element is raw text: it ends at the first
``</script`` (or ``</style``) followed by whitespace, ``/`` or ``>``, whatever
string or comment it appears inside — which is what a browser does too.
Comments, ``<!…>`` declarations and ``<?…>`` instructions are skipped whole, so
a ``<script>`` inside a comment is not an element. An element left open runs to
the end of the input.

**What it deliberately leaves alone.** ``<noscript>``, inline event handlers,
other attributes and comments all stay in the digest. None of them varies
between fetches of any document in the corpus today, and widening the set would
be a broader claim than the evidence supports. If a CDN starts injecting a
rotating token into one of them, the digest moves again — waste, never a hidden
revision — and the nightly live check against Aetna is what notices.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from typing import Final

#: Elements whose content is not document text. The single definition: the
#: digest ignores exactly these, and text extraction discards exactly these.
NON_TEXT_ELEMENTS: Final[frozenset[str]] = frozenset({"script", "style"})

#: HTML's whitespace characters.
_WHITESPACE: Final = frozenset(b" \t\n\r\f")

#: What ends a tag name: whitespace, or the start of ``/>`` or ``>``.
_TAG_NAME_END: Final = _WHITESPACE | frozenset(b"/>")

_LT: Final = ord("<")
_GT: Final = ord(">")
_EQUALS: Final = ord("=")
_QUOTES: Final = frozenset(b"\"'")
_SLASH: Final = ord("/")
#: ``<!`` opens a comment, doctype or CDATA section; ``<?`` a processing
#: instruction. Outside a comment, both run to the next ``>``.
_DECLARATION_OPENERS: Final = frozenset(b"!?")

#: The end tag that closes each raw-text element, matched the way a browser
#: matches it: the name in any case, then whitespace, ``/`` or ``>``.
_END_TAGS: Final[dict[bytes, re.Pattern[bytes]]] = {
    name.encode("ascii"): re.compile(rb"</" + name.encode("ascii") + rb"[ \t\n\r\f/>]", re.I)
    for name in NON_TEXT_ELEMENTS
}


def _is_ascii_letter(byte: int) -> bool:
    return 0x41 <= byte <= 0x5A or 0x61 <= byte <= 0x7A


def _comment_end(data: bytes, opening: int) -> int:
    """Return the offset just past the end of the comment opened at ``opening``.

    HTML ends a comment at ``-->`` or at ``--!>``, whichever comes first.
    ``-->`` is searched for from the opener's own dashes so that ``<!-->`` and
    ``<!--->`` close themselves, as the tokenizer specifies; ``--!>`` only after
    them, so ``<!--!>`` does not. An unclosed comment runs to the end of the input.
    """
    ends = [
        found + len(marker)
        for marker, search_from in ((b"-->", opening + 2), (b"--!>", opening + 4))
        if (found := data.find(marker, search_from)) >= 0
    ]
    return min(ends, default=len(data))


def _tag_end(data: bytes, start: int) -> int:
    """Return the offset just past the ``>`` that closes a tag, from ``start``.

    A quoted attribute value may contain ``>``, so quotes are honoured — but only
    where a value can begin, after ``=`` and optional whitespace, as in HTML. A
    tag with no closing ``>`` runs to the end of the input.
    """
    end = len(data)
    close = data.find(b">", start)
    if close < 0:
        return end
    if b'"' not in data[start:close] and b"'" not in data[start:close]:
        # Nearly every tag: no quote before the first ">", so that ">" ends it.
        return close + 1

    position = start
    value_can_start = False
    while position < end:
        byte = data[position]
        if byte == _GT:
            return position + 1
        if value_can_start and byte in _QUOTES:
            closing_quote = data.find(bytes((byte,)), position + 1)
            if closing_quote < 0:
                return end
            position = closing_quote + 1
            value_can_start = False
            continue
        if byte == _EQUALS:
            value_can_start = True
        elif byte not in _WHITESPACE:
            value_can_start = False
        position += 1
    return end


def non_text_spans(data: bytes) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` for each script or style element, in order.

    Each span runs from the ``<`` of the start tag to just past the ``>`` of the
    end tag, so ``data[start:end]`` is the whole element. Spans never overlap.
    """
    end = len(data)
    position = 0
    while True:
        opening = data.find(b"<", position)
        if opening < 0 or opening + 1 >= end:
            return
        following = data[opening + 1]

        if data.startswith(b"<!--", opening):
            position = _comment_end(data, opening)
        elif following in _DECLARATION_OPENERS:
            close = data.find(b">", opening + 2)
            position = end if close < 0 else close + 1
        elif following == _SLASH:
            # An end tag outside any raw-text element, including a stray
            # "</script>" in a fragment. It removes nothing.
            position = _tag_end(data, opening + 2)
        elif _is_ascii_letter(following):
            name_end = opening + 1
            while name_end < end and data[name_end] not in _TAG_NAME_END:
                name_end += 1
            name = data[opening + 1 : name_end].lower()
            start_tag_end = _tag_end(data, name_end)
            end_tag = _END_TAGS.get(name)
            if end_tag is None:
                position = start_tag_end
                continue
            match = end_tag.search(data, start_tag_end)
            element_end = end if match is None else _tag_end(data, match.start() + 2 + len(name))
            yield opening, element_end
            position = element_end
        else:
            # A "<" that opens nothing, such as "a < b" in prose.
            position = opening + 1


def strip_non_text(data: bytes) -> bytes:
    """Return ``data`` with every script and style element cut out.

    Only those byte ranges are removed; every other byte is kept exactly as it
    was. A document with neither comes back unchanged, which is what keeps the
    digest of script-free HTML equal to the SHA-256 of its published bytes.
    """
    kept: list[bytes] = []
    position = 0
    for start, end in non_text_spans(data):
        kept.append(data[position:start])
        position = end
    if not kept:
        return data
    kept.append(data[position:])
    return b"".join(kept)


def html_digest(data: bytes) -> str:
    """Return the SHA-256 hex digest of an HTML document, script and style excluded.

    This is ``insurance_policies.content_hash`` for every ``text/html``
    document. Both the ingest endpoint and the scraper's pre-upload skip call it,
    so the two cannot disagree about what a document's digest is.
    """
    return hashlib.sha256(strip_non_text(data)).hexdigest()

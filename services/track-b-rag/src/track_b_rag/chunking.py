"""Splitting policy text into retrievable chunks.

800 characters with 150 of overlap, per TASK-011. The overlap is what keeps a
criterion that straddles a boundary — "...documented failure of six weeks of |
conservative therapy..." — retrievable from either side, which matters because
prior authorization criteria are exactly the kind of sentence that gets split.

The splitter is a cached singleton. It holds no state between calls; the cache
is there because constructing it compiles its separator handling and there is no
reason to redo that per request.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_text_splitters import RecursiveCharacterTextSplitter

#: TASK-011 fixes both numbers. They are module constants rather than settings:
#: changing either invalidates every chunk already indexed, so it is a
#: re-ingestion decision, not something to vary per environment.
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150


@lru_cache(maxsize=1)
def get_splitter() -> RecursiveCharacterTextSplitter:
    """Return the process-wide text splitter."""
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )


def chunk_text(text: str) -> list[str]:
    """Split document text into overlapping chunks, dropping empty ones.

    Returns an empty list for text that is empty or only whitespace — the caller
    treats that as "this PDF yielded nothing to index" and rejects the upload
    rather than writing a policy row with no vectors behind it.
    """
    if not text.strip():
        return []
    return [chunk for chunk in get_splitter().split_text(text) if chunk.strip()]

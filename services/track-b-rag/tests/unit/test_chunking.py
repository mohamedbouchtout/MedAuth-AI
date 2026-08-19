"""Chunk geometry — 800 characters with 150 of overlap, per TASK-011."""

from __future__ import annotations

from track_b_rag.chunking import CHUNK_OVERLAP, CHUNK_SIZE, chunk_text, get_splitter


def test_the_geometry_is_what_the_task_specifies() -> None:
    assert (CHUNK_SIZE, CHUNK_OVERLAP) == (800, 150)


def test_short_text_stays_one_chunk() -> None:
    assert chunk_text("Prior authorization required for CPT 72148.") == [
        "Prior authorization required for CPT 72148."
    ]


def test_long_text_is_split() -> None:
    chunks = chunk_text(" ".join(f"criterion-{index}" for index in range(500)))

    assert len(chunks) > 1


def test_no_chunk_exceeds_the_configured_size() -> None:
    chunks = chunk_text("\n".join(f"Documented failure of therapy {i}." for i in range(400)))

    assert chunks
    assert max(len(chunk) for chunk in chunks) <= CHUNK_SIZE


def test_consecutive_chunks_overlap() -> None:
    """The overlap is what keeps a criterion split across a boundary retrievable."""
    text = " ".join(f"word{index}" for index in range(1000))

    chunks = chunk_text(text)

    tail = chunks[0][-CHUNK_OVERLAP:]
    shared = [word for word in tail.split() if word and word in chunks[1]]
    assert shared


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("") == []


def test_whitespace_only_text_yields_no_chunks() -> None:
    assert chunk_text("   \n\n\t  ") == []


def test_blank_chunks_are_dropped() -> None:
    assert all(chunk.strip() for chunk in chunk_text("Alpha.\n\n\n\n\n\nBeta."))


def test_the_splitter_is_a_singleton() -> None:
    assert get_splitter() is get_splitter()

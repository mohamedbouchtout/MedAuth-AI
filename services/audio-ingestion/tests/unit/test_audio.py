"""The in-memory audio buffer: chunking, the ceiling, and the clearing.

Most of this is ordinary accumulator behaviour. Two of these tests are not: the
one that proves nothing is left behind after ``clear()``, and the one that proves
the ceiling stops an unbounded accumulation of PHI when the consumer stalls.
"""

from __future__ import annotations

import pytest

from src.audio import DEFAULT_FLUSH_THRESHOLD_BYTES, AudioBuffer, AudioBufferOverflow


def test_nothing_is_released_below_the_chunk_threshold() -> None:
    buffer = AudioBuffer(flush_threshold_bytes=100)
    buffer.write(b"x" * 99)

    assert list(buffer.take_chunks()) == []
    assert buffer.pending_bytes == 99


def test_a_whole_chunk_is_released_and_the_remainder_kept() -> None:
    buffer = AudioBuffer(flush_threshold_bytes=100)
    buffer.write(b"a" * 130)

    assert list(buffer.take_chunks()) == [b"a" * 100]
    assert buffer.pending_bytes == 30


def test_several_small_frames_coalesce_into_one_chunk() -> None:
    """The reason the buffer exists: a client's frame size is not ours."""
    buffer = AudioBuffer(flush_threshold_bytes=10)
    for _ in range(5):
        buffer.write(b"ab")

    assert list(buffer.take_chunks()) == [b"ababababab"]


def test_one_large_frame_becomes_several_chunks_in_order() -> None:
    buffer = AudioBuffer(flush_threshold_bytes=4)
    buffer.write(b"0123456789")

    assert list(buffer.take_chunks()) == [b"0123", b"4567"]
    assert buffer.drain() == b"89"


def test_audio_is_reassembled_byte_for_byte() -> None:
    """Chunk boundaries must not lose or reorder a single sample."""
    buffer = AudioBuffer(flush_threshold_bytes=7)
    original = bytes(range(256)) * 3
    released = bytearray()

    for start in range(0, len(original), 13):
        buffer.write(original[start : start + 13])
        for chunk in buffer.take_chunks():
            released += chunk
    released += buffer.drain()

    assert bytes(released) == original


def test_draining_empties_the_buffer() -> None:
    buffer = AudioBuffer(flush_threshold_bytes=100)
    buffer.write(b"tail")

    assert buffer.drain() == b"tail"
    assert buffer.pending_bytes == 0
    assert buffer.drain() == b""


def test_clear_discards_held_audio_and_releases_the_underlying_buffer() -> None:
    """The point where "audio never persists" is actually enforced."""
    buffer = AudioBuffer(flush_threshold_bytes=1000)
    buffer.write(b"patient audio")

    buffer.clear()

    assert buffer.pending_bytes == 0
    # The BytesIO is closed, not merely emptied — nothing is left holding the
    # bytes, and a later write would raise rather than silently reopen it.
    with pytest.raises(ValueError):
        buffer.write(b"more")


def test_the_ceiling_stops_an_oversized_frame() -> None:
    """One enormous frame must not become one enormous allocation of PHI."""
    buffer = AudioBuffer(flush_threshold_bytes=1_000, max_bytes=100)

    with pytest.raises(AudioBufferOverflow):
        buffer.write(b"z" * 101)


def test_the_ceiling_counts_what_is_held_not_what_has_passed_through() -> None:
    """Chunks already released are gone; only what is still held counts."""
    buffer = AudioBuffer(flush_threshold_bytes=10, max_bytes=25)
    buffer.write(b"y" * 20)
    list(buffer.take_chunks())

    buffer.write(b"y" * 20)  # would exceed 25 if released bytes still counted

    assert buffer.pending_bytes == 20


def test_the_default_chunk_is_250ms_of_the_format_the_clients_capture() -> None:
    """16kHz, signed 16-bit, mono — the frame size TASK-022 and TASK-023 send."""
    assert DEFAULT_FLUSH_THRESHOLD_BYTES == 16_000 * 2 // 4

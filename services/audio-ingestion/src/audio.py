"""The in-memory audio buffer. Nothing here ever touches a filesystem.

CLAUDE.md's Key Architectural Constraints say audio never persists: it is
processed in memory and discarded. This module is the only thing in the service
that holds audio at all, so the constraint is enforceable by reading one file.

**Why buffer if the audio is being forwarded anyway.** A client sends whatever
frame size its capture API produces, and Transcribe bills a signed event per
chunk. Very small frames waste signatures and round trips; very large ones add
latency to a nudge budget measured in seconds. The buffer accumulates client
frames and releases them at a fixed size, which decouples the two.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Final

#: Bytes per released chunk. At 16kHz signed 16-bit mono, 8000 bytes is 250ms —
#: the frame size TASK-022 and TASK-023 both stream at, so a well-behaved client
#: usually flushes exactly one chunk per frame and the buffer stays empty.
DEFAULT_FLUSH_THRESHOLD_BYTES: Final = 8_000

#: Ceiling on how much audio one connection may hold at once. Frames are read and
#: forwarded sequentially, so this is not reached by a client streaming faster
#: than Transcribe accepts — it is what stops a single oversized frame from being
#: accumulated. 5MB is roughly two and a half minutes of the expected format,
#: far above any legitimate frame and far below anything that threatens the pod.
MAX_BUFFERED_BYTES: Final = 5_000_000


class AudioBufferOverflow(Exception):
    """The client outran the transcription stream by more than the ceiling allows."""


class AudioBuffer:
    """A bounded in-memory accumulator over ``BytesIO``.

    Not thread-safe and not intended to be: one instance belongs to one
    WebSocket connection, driven by one task.
    """

    def __init__(
        self,
        *,
        flush_threshold_bytes: int = DEFAULT_FLUSH_THRESHOLD_BYTES,
        max_bytes: int = MAX_BUFFERED_BYTES,
    ) -> None:
        self._buffer = io.BytesIO()
        self._flush_threshold = flush_threshold_bytes
        self._max_bytes = max_bytes
        self._pending = 0

    @property
    def pending_bytes(self) -> int:
        """How much audio is held and not yet released."""
        return self._pending

    def write(self, chunk: bytes) -> None:
        """Add a client frame to the buffer.

        Raises:
            AudioBufferOverflow: The ceiling would be exceeded. The caller closes
                the connection rather than trimming — silently dropping part of a
                clinical encounter is worse than refusing it visibly.
        """
        if self._pending + len(chunk) > self._max_bytes:
            raise AudioBufferOverflow(
                f"connection would hold more than {self._max_bytes} bytes of audio at once"
            )
        self._buffer.write(chunk)
        self._pending += len(chunk)

    def take_chunks(self) -> Iterator[bytes]:
        """Yield every whole chunk available, leaving the remainder buffered.

        A generator so a caller can await between chunks without the buffer
        having to know that it is being consumed asynchronously.
        """
        while self._pending >= self._flush_threshold:
            yield self._take(self._flush_threshold)

    def drain(self) -> bytes:
        """Return everything still held, leaving the buffer empty.

        Called once when the client disconnects, so the tail of the encounter is
        transcribed rather than discarded for being shorter than a chunk.
        """
        return self._take(self._pending) if self._pending else b""

    def clear(self) -> None:
        """Discard any audio held and release the underlying buffer.

        Explicit rather than left to garbage collection: this is the point where
        the constraint "audio is discarded immediately after transcription" is
        actually met, and a caller should be able to see it happen in a
        ``finally`` block.
        """
        self._buffer.seek(0)
        self._buffer.truncate(0)
        self._buffer.close()
        self._pending = 0

    def _take(self, size: int) -> bytes:
        """Remove and return the first ``size`` bytes held."""
        self._buffer.seek(0)
        taken = self._buffer.read(size)
        remainder = self._buffer.read()
        self._buffer.seek(0)
        self._buffer.truncate(0)
        self._buffer.write(remainder)
        self._pending = len(remainder)
        return taken

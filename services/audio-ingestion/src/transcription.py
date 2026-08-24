"""The seam between the WebSocket route and whatever transcribes the audio.

The route depends on this protocol and never on AWS. That is what lets the unit
suite drive a full connection — handshake, audio frames, published segments,
teardown — against an injected fake in milliseconds, the same arrangement
track-b-rag uses for Bedrock, Qdrant and the embedding model.

It is a seam and not an abstraction layer: there is exactly one real
implementation (:mod:`src.transcribe_medical`) and no intention of a second. It
exists for testability, so it stays as narrow as the route's actual needs —
push audio, read segments, stop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TranscriptSegment:
    """One transcription result.

    ``text`` is PHI: it is what was said in a clinical encounter. It is carried
    to Redis and never to a log line.
    """

    result_id: str
    text: str
    is_partial: bool
    start_time: float | None = None
    end_time: float | None = None


class TranscriptionStream(Protocol):
    """One live transcription of one encounter."""

    async def send_audio(self, chunk: bytes) -> None:
        """Push a chunk of audio at the transcriber."""
        ...

    async def end_input(self) -> None:
        """Signal that no more audio is coming.

        The segment iterator finishes once the transcriber has drained what it
        was already given, so this is what makes ``segments()`` terminate.
        """
        ...

    async def close(self) -> None:
        """Release the stream. Must be safe to call after ``end_input``."""
        ...

    def segments(self) -> AsyncIterator[TranscriptSegment]:
        """Yield results as the transcriber produces them."""
        ...


#: Builds a stream per connection. A callable rather than a client object so the
#: route holds nothing that outlives one encounter.
TranscriptionStreamFactory = Callable[[], Awaitable[TranscriptionStream]]

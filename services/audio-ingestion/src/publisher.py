"""Publication of transcript segments onto the Redis bus.

The channel is ``transcription:{session_id}``, from CLAUDE.md's canonical Redis
key list, and it has two independent consumers: track-a-clinical accumulates the
full transcript for SOAP generation (TASK-030) and track-b-rag scans it for
procedure-order keywords (TASK-021). Neither knows about the other; this service
publishes once and Redis fans out.

**Only stabilized results are published.** Transcribe emits a partial result for
an utterance repeatedly as it revises it — the same ``result_id``, growing and
changing, several times a second — and then one final result with
``is_partial`` false. Forwarding the partials would multiply bus traffic by an
order of magnitude and, worse, make TASK-021 fire the same procedure keyword
over and over as the same sentence is re-transcribed, turning one order into a
stream of duplicate nudges. The payload still carries ``is_partial`` so that a
later task can widen this to partials without changing the message shape or the
consumers' parsing.

**The payload is PHI.** ``text`` is what was said during a clinical encounter.
It goes to Redis and nowhere else: this module logs that a segment was
published and never what was in it.
"""

from __future__ import annotations

import json
import logging
from typing import Final

from redis.asyncio import Redis

from src.transcription import TranscriptSegment

logger = logging.getLogger(__name__)

#: The canonical channel pattern. Formatted here rather than at call sites so a
#: variant spelling cannot appear in one place and go unnoticed.
CHANNEL_TEMPLATE: Final = "transcription:{session_id}"


def channel_for(session_id: str) -> str:
    """Return the transcript channel for one session."""
    return CHANNEL_TEMPLATE.format(session_id=session_id)


def encode_segment(segment: TranscriptSegment, *, session_id: str) -> str:
    """Serialize one segment for the bus.

    ``session_id`` is repeated inside the payload as well as being in the channel
    name so a consumer that multiplexes several sessions onto one connection does
    not have to parse it back out of the channel.
    """
    return json.dumps(
        {
            "session_id": session_id,
            "result_id": segment.result_id,
            "text": segment.text,
            "is_partial": segment.is_partial,
            "start_time": segment.start_time,
            "end_time": segment.end_time,
        }
    )


async def publish_segment(
    redis: Redis,
    segment: TranscriptSegment,
    *,
    session_id: str,
) -> bool:
    """Publish one segment, returning whether it was actually sent.

    Partial results return False without touching Redis — see the module
    docstring for why they are dropped rather than forwarded.
    """
    if segment.is_partial:
        return False

    await redis.publish(channel_for(session_id), encode_segment(segment, session_id=session_id))
    # Length rather than content: the operational question is whether the bus is
    # moving, and the content is PHI.
    logger.debug("Published transcript segment of %d characters", len(segment.text))
    return True


async def check_health(redis: Redis) -> bool:
    """Return whether Redis answers. Used by ``GET /health``."""
    try:
        await redis.ping()
    except Exception:
        logger.warning("Redis health check failed", exc_info=True)
        return False
    return True

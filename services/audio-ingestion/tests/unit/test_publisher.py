"""What goes on the transcript bus, and what deliberately does not."""

from __future__ import annotations

import json

from src.publisher import channel_for, check_health, encode_segment, publish_segment
from src.transcription import TranscriptSegment


class FakeRedis:
    def __init__(self, *, healthy: bool = True) -> None:
        self.published: list[tuple[str, str]] = []
        self.healthy = healthy

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("redis unreachable")
        return True


def segment(*, is_partial: bool = False) -> TranscriptSegment:
    return TranscriptSegment(
        result_id="r1",
        text="Order an MRI of the left knee.",
        is_partial=is_partial,
        start_time=1.5,
        end_time=4.0,
    )


def test_the_channel_matches_the_canonical_redis_key() -> None:
    """CLAUDE.md's key list is the contract TASK-021 and TASK-030 subscribe on."""
    assert channel_for("abc-123") == "transcription:abc-123"


async def test_a_stabilized_segment_is_published_on_that_channel() -> None:
    redis = FakeRedis()

    published = await publish_segment(redis, segment(), session_id="s-1")

    assert published is True
    assert redis.published[0][0] == "transcription:s-1"


async def test_a_partial_segment_is_dropped_without_touching_redis() -> None:
    """Forwarding revisions would make TASK-021 fire on the same order repeatedly."""
    redis = FakeRedis()

    published = await publish_segment(redis, segment(is_partial=True), session_id="s-1")

    assert published is False
    assert redis.published == []


def test_the_payload_carries_everything_a_consumer_needs() -> None:
    """Including ``session_id``, so a multiplexing consumer need not parse the channel."""
    decoded = json.loads(encode_segment(segment(), session_id="s-1"))

    assert decoded == {
        "session_id": "s-1",
        "result_id": "r1",
        "text": "Order an MRI of the left knee.",
        "is_partial": False,
        "start_time": 1.5,
        "end_time": 4.0,
    }


def test_the_payload_is_json_a_consumer_can_parse_without_this_package() -> None:
    """Plain JSON, not pickle or a Pydantic dump — consumers are separate services."""
    raw = encode_segment(segment(), session_id="s-1")

    assert isinstance(raw, str)
    assert json.loads(raw)["text"]


async def test_health_reports_true_when_redis_answers() -> None:
    assert await check_health(FakeRedis()) is True


async def test_health_reports_false_rather_than_raising() -> None:
    """``GET /health`` needs a flag, not an exception to turn into a 500."""
    assert await check_health(FakeRedis(healthy=False)) is False


async def test_nothing_published_when_the_transcript_is_partial_even_if_long() -> None:
    """The rule is the flag, not the length — a long partial is still a partial."""
    redis = FakeRedis()
    long_partial = TranscriptSegment(result_id="r9", text="x" * 5_000, is_partial=True)

    assert await publish_segment(redis, long_partial, session_id="s-1") is False
    assert redis.published == []

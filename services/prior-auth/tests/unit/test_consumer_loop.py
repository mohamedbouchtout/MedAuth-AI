"""The consumer's read loop: subscribing, staying healthy, and reconnecting.

Separate from ``test_consumer`` because these drive the task rather than calling
``handle_message`` directly, and they need a Redis fake rather than a pub/sub
one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from redis.exceptions import RedisError

from prior_auth.consumer import SessionEndConsumer


class FakePubSub:
    """Hands the loop a scripted sequence of messages, then nothing."""

    def __init__(self, messages: list[dict[str, Any] | None], *, fail_after: int | None = None):
        self.messages = list(messages)
        self.fail_after = fail_after
        self.reads = 0
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False
        self.drained = asyncio.Event()

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.unsubscribed.extend(channels)

    async def get_message(self, **_kwargs: Any) -> dict[str, Any] | None:
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise RedisError("connection lost")
        if self.messages:
            return self.messages.pop(0)
        self.drained.set()
        # Yield to the loop rather than spinning, so a cancel lands promptly.
        await asyncio.sleep(0)
        return None

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    def __init__(self, pubsubs: list[FakePubSub]) -> None:
        self._pubsubs = list(pubsubs)
        self.handed_out: list[FakePubSub] = []

    def pubsub(self) -> FakePubSub:
        made = self._pubsubs.pop(0) if self._pubsubs else FakePubSub([])
        self.handed_out.append(made)
        return made


def started(session_id: uuid.UUID) -> dict[str, Any]:
    return {
        "channel": "sessions:started",
        "data": json.dumps({"session_id": str(session_id)}),
    }


class TestTheLoop:
    async def test_it_subscribes_to_the_announcement_channel(self) -> None:
        pubsub = FakePubSub([])
        consumer = SessionEndConsumer(FakeRedis([pubsub]))  # type: ignore[arg-type]

        consumer.start()
        await asyncio.wait_for(pubsub.drained.wait(), timeout=1)

        assert pubsub.subscribed == ["sessions:started"]
        assert consumer.is_healthy() is True
        await consumer.stop()

    async def test_start_is_idempotent(self) -> None:
        """A second start must not open a second subscription to the same channel."""
        pubsub = FakePubSub([])
        redis = FakeRedis([pubsub])
        consumer = SessionEndConsumer(redis)  # type: ignore[arg-type]

        consumer.start()
        consumer.start()
        await asyncio.wait_for(pubsub.drained.wait(), timeout=1)

        assert len(redis.handed_out) == 1
        await consumer.stop()

    async def test_an_announcement_read_off_the_loop_is_watched(self) -> None:
        session_id = uuid.uuid4()
        pubsub = FakePubSub([started(session_id)])
        consumer = SessionEndConsumer(FakeRedis([pubsub]))  # type: ignore[arg-type]

        consumer.start()
        await asyncio.wait_for(pubsub.drained.wait(), timeout=1)

        assert consumer.watched_sessions == frozenset({session_id})
        await consumer.stop()

    async def test_stopping_leaves_the_consumer_unhealthy_and_closes_the_subscription(
        self,
    ) -> None:
        pubsub = FakePubSub([])
        consumer = SessionEndConsumer(FakeRedis([pubsub]))  # type: ignore[arg-type]

        consumer.start()
        await asyncio.wait_for(pubsub.drained.wait(), timeout=1)
        await consumer.stop()

        assert consumer.is_healthy() is False
        assert pubsub.closed is True


class TestReconnect:
    async def test_a_dropped_connection_is_reported_and_resubscribed(self, caplog: Any) -> None:
        """The sessions in flight are lost, and the log says how many.

        Their encounters produce no prior-authorization request, and nothing
        else in the system would say so.
        """
        session_id = uuid.uuid4()
        first = FakePubSub([started(session_id)], fail_after=2)
        second = FakePubSub([])
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        consumer = SessionEndConsumer(
            FakeRedis([first, second]),  # type: ignore[arg-type]
            sleep=sleep,
        )

        with caplog.at_level(logging.WARNING):
            consumer.start()
            await asyncio.wait_for(second.drained.wait(), timeout=1)

        assert "lost its Redis connection" in caplog.text
        assert "1 session(s)" in caplog.text
        assert second.subscribed == ["sessions:started"]
        assert slept == [2.0]
        await consumer.stop()

    async def test_the_watch_set_is_cleared_on_a_drop(self) -> None:
        """A resubscribed consumer has not re-learned the old sessions."""
        first = FakePubSub([started(uuid.uuid4())], fail_after=2)
        second = FakePubSub([])

        async def sleep(_seconds: float) -> None:
            return None

        consumer = SessionEndConsumer(
            FakeRedis([first, second]),  # type: ignore[arg-type]
            sleep=sleep,
        )
        consumer.start()
        await asyncio.wait_for(second.drained.wait(), timeout=1)

        assert consumer.watched_sessions == frozenset()
        await consumer.stop()

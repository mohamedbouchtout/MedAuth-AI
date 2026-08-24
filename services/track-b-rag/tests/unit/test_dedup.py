"""The once-per-encounter guard: atomicity, cleanup, and failing open."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from redis.exceptions import RedisError

from track_b_rag import dedup
from track_b_rag.dedup import (
    PROCEDURE_SEEN_TTL_SECONDS,
    claim_procedure,
    forget_session,
    procedure_seen_key,
    release_procedure,
)


class FakePipeline:
    """Buffers commands the way redis-py does, then applies them on execute."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple[Any, ...]]] = []

    def sadd(self, key: str, member: str) -> FakePipeline:
        self._queued.append(("sadd", (key, member)))
        return self

    def expire(self, key: str, ttl: int) -> FakePipeline:
        self._queued.append(("expire", (key, ttl)))
        return self

    async def execute(self) -> list[Any]:
        if self._redis.failing:
            raise RedisError("redis is down")
        results: list[Any] = []
        for command, args in self._queued:
            if command == "sadd":
                key, member = args
                members = self._redis.sets.setdefault(key, set())
                results.append(int(member not in members))
                members.add(member)
            else:
                key, ttl = args
                self._redis.expiries[key] = ttl
                results.append(True)
        return results


class FakeRedis:
    """Enough of a Redis for set membership, expiry and deletion."""

    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.sets: dict[str, set[str]] = {}
        self.expiries: dict[str, int] = {}

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def srem(self, key: str, member: str) -> int:
        if self.failing:
            raise RedisError("redis is down")
        members = self.sets.get(key, set())
        removed = member in members
        members.discard(member)
        return int(removed)

    async def delete(self, key: str) -> int:
        if self.failing:
            raise RedisError("redis is down")
        return int(self.sets.pop(key, None) is not None)


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def session_id() -> uuid.UUID:
    return uuid.uuid4()


def test_the_key_is_the_canonical_pattern(session_id: uuid.UUID) -> None:
    assert procedure_seen_key(session_id) == f"procedure_seen:{session_id}"


async def test_the_first_mention_is_claimed(redis: FakeRedis, session_id: uuid.UUID) -> None:
    assert await claim_procedure(redis, session_id, "MRI") is True  # type: ignore[arg-type]


async def test_a_repeat_mention_is_suppressed(redis: FakeRedis, session_id: uuid.UUID) -> None:
    """One order should raise one nudge, however many times it is said aloud."""
    await claim_procedure(redis, session_id, "MRI")  # type: ignore[arg-type]

    assert await claim_procedure(redis, session_id, "MRI") is False  # type: ignore[arg-type]


async def test_a_different_procedure_in_the_same_session_still_claims(
    redis: FakeRedis, session_id: uuid.UUID
) -> None:
    await claim_procedure(redis, session_id, "MRI")  # type: ignore[arg-type]

    assert await claim_procedure(redis, session_id, "biopsy") is True  # type: ignore[arg-type]


async def test_the_same_procedure_in_another_session_still_claims(redis: FakeRedis) -> None:
    """The guard is per encounter — the next patient gets their own nudge."""
    await claim_procedure(redis, uuid.uuid4(), "MRI")  # type: ignore[arg-type]

    assert await claim_procedure(redis, uuid.uuid4(), "MRI") is True  # type: ignore[arg-type]


async def test_the_claim_sets_a_ttl_as_a_safety_net(
    redis: FakeRedis, session_id: uuid.UUID
) -> None:
    """A visit that never ends must not leave its guard behind for good."""
    await claim_procedure(redis, session_id, "MRI")  # type: ignore[arg-type]

    assert redis.expiries[procedure_seen_key(session_id)] == PROCEDURE_SEEN_TTL_SECONDS


async def test_an_unreachable_redis_fails_open(
    session_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    """A duplicate nudge is noise; a suppressed one is a procedure nobody flagged."""
    failing = FakeRedis(failing=True)

    assert await claim_procedure(failing, session_id, "MRI") is True  # type: ignore[arg-type]
    assert "treating it as a first mention" in caplog.text


async def test_a_released_claim_can_be_taken_again(redis: FakeRedis, session_id: uuid.UUID) -> None:
    """A query that failed transiently must not silence the rest of the visit."""
    await claim_procedure(redis, session_id, "MRI")  # type: ignore[arg-type]
    await release_procedure(redis, session_id, "MRI")  # type: ignore[arg-type]

    assert await claim_procedure(redis, session_id, "MRI") is True  # type: ignore[arg-type]


async def test_a_failed_release_is_logged_and_not_raised(
    session_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    failing = FakeRedis(failing=True)

    await release_procedure(failing, session_id, "MRI")  # type: ignore[arg-type]

    assert "the claim stands" in caplog.text


async def test_forgetting_a_session_drops_its_guard(
    redis: FakeRedis, session_id: uuid.UUID
) -> None:
    await claim_procedure(redis, session_id, "MRI")  # type: ignore[arg-type]

    await forget_session(redis, session_id)  # type: ignore[arg-type]

    assert procedure_seen_key(session_id) not in redis.sets


async def test_a_failed_cleanup_is_logged_and_not_raised(
    session_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    failing = FakeRedis(failing=True)

    await forget_session(failing, session_id)  # type: ignore[arg-type]

    assert "will expire on its TTL" in caplog.text


def test_the_ttl_is_a_constant_not_a_setting() -> None:
    """CLAUDE.md's stance: a knob nobody is meant to turn eventually disagrees."""
    assert dedup.PROCEDURE_SEEN_TTL_SECONDS == 4 * 60 * 60

"""The policy-rules cache: the key it builds, and how it behaves when Redis is not there.

The key shape is a cross-service contract — CLAUDE.md's canonical Redis key list
fixes it, and the operations team greps for it — so it is asserted literally
rather than rebuilt from the same f-string the implementation uses.

The rest of this module is about degradation. A cache is not a correctness
dependency here: every read and write must survive Redis being down, because a
failed nudge during a live encounter is worse than an expensive one.
"""

from __future__ import annotations

import pytest
from redis.exceptions import RedisError

from track_b_rag import cache
from track_b_rag.config import get_settings


class RecordingRedis:
    """A Redis stand-in that can be told to fail, and remembers what it was asked."""

    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing
        self.store: dict[str, str] = {}
        self.expiries: dict[str, int | None] = {}
        self.pinged = 0

    async def ping(self) -> bool:
        self.pinged += 1
        if self.failing:
            raise ConnectionError("redis is down")
        return True

    async def get(self, key: str) -> bytes | None:
        if self.failing:
            raise RedisError("redis is down")
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        if self.failing:
            raise RedisError("redis is down")
        self.store[key] = value
        self.expiries[key] = ex
        return True


@pytest.fixture
def redis() -> RecordingRedis:
    return RecordingRedis()


def test_the_key_is_the_one_claude_md_fixes() -> None:
    key = cache.policy_rules_key(payer="Aetna", plan_type="PPO", state="MA", cpt_code="73721")

    assert key == "rag:Aetna:PPO:MA:73721"


def test_the_key_mentions_no_patient() -> None:
    """The whole two-stage split rests on this: the key names a plan, not a person.

    Anything cached under it is shared by every patient on that plan, so a value
    that varied by patient would be served to the wrong one.
    """
    key = cache.policy_rules_key(payer="Aetna", plan_type="PPO", state="MA", cpt_code="73721")

    assert key.split(":") == ["rag", "Aetna", "PPO", "MA", "73721"]


def test_the_ttl_is_a_day() -> None:
    assert cache.POLICY_RULES_TTL_SECONDS == 86_400


async def test_a_write_then_a_read_round_trips(redis: RecordingRedis) -> None:
    assert await cache.set_cached(redis, "rag:k", '{"a": 1}', 60) is True

    assert await cache.get_cached(redis, "rag:k") == '{"a": 1}'


async def test_a_write_carries_the_ttl_it_was_given(redis: RecordingRedis) -> None:
    await cache.set_cached(redis, "rag:k", "{}", cache.POLICY_RULES_TTL_SECONDS)

    assert redis.expiries["rag:k"] == 86_400


async def test_an_absent_key_is_a_miss(redis: RecordingRedis) -> None:
    assert await cache.get_cached(redis, "rag:nothing") is None


async def test_a_string_value_survives_a_client_that_decodes_responses(
    redis: RecordingRedis,
) -> None:
    """``Redis.from_url(decode_responses=True)`` yields str, not bytes.

    The service does not set that flag today, but a client configured elsewhere
    would otherwise turn every cache hit into ``"b'{...}'"`` — a string that
    parses as nothing.
    """
    redis.store["rag:k"] = '{"a": 1}'

    async def get_str(key: str) -> str:
        return redis.store[key]

    redis.get = get_str  # type: ignore[method-assign,assignment]

    assert await cache.get_cached(redis, "rag:k") == '{"a": 1}'


async def test_an_unreachable_redis_reads_as_a_miss(caplog: pytest.LogCaptureFixture) -> None:
    """The caller's answer to a miss is to do the work, which is what we want here."""
    redis = RecordingRedis(failing=True)

    with caplog.at_level("WARNING"):
        assert await cache.get_cached(redis, "rag:k") is None

    assert "cache miss" in caplog.text


async def test_an_unreachable_redis_fails_a_write_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The computed answer is still correct; it just will not be reused."""
    redis = RecordingRedis(failing=True)

    with caplog.at_level("WARNING"):
        assert await cache.set_cached(redis, "rag:k", "{}", 60) is False

    assert "not cached" in caplog.text


async def test_a_failed_read_logs_the_key_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The key holds payer and procedure identifiers. There is no patient in it."""
    redis = RecordingRedis(failing=True)

    with caplog.at_level("WARNING"):
        await cache.get_cached(redis, "rag:Aetna:PPO:MA:73721")

    assert "rag:Aetna:PPO:MA:73721" in caplog.text


async def test_health_is_a_ping(redis: RecordingRedis) -> None:
    assert await cache.check_health(redis) is True
    assert redis.pinged == 1


async def test_health_is_false_when_redis_does_not_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = RecordingRedis(failing=True)

    with caplog.at_level("WARNING"):
        assert await cache.check_health(redis) is False

    assert "Redis health check failed" in caplog.text


def test_the_client_is_one_lazily_created_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing connects at import; the first call builds it and every later one reuses it.

    ``REDIS_URL`` is set explicitly rather than left to the environment: CI and a
    developer's shell disagree about the host, and the assertion here is that the
    setting is read, not which value it happens to hold.
    """
    monkeypatch.setenv("REDIS_URL", "redis://cache.example:6379/3")
    get_settings.cache_clear()
    cache.get_client.cache_clear()
    built: list[str] = []

    class FakeRedisModule:
        @staticmethod
        def from_url(url: str) -> object:
            built.append(url)
            return object()

    monkeypatch.setattr(cache, "Redis", FakeRedisModule)

    first = cache.get_client()
    second = cache.get_client()

    assert first is second
    assert built == ["redis://cache.example:6379/3"]
    cache.get_client.cache_clear()
    get_settings.cache_clear()


async def test_closing_releases_the_client_and_forgets_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache.get_client.cache_clear()
    closed: list[bool] = []

    class Closable:
        async def aclose(self) -> None:
            closed.append(True)

    class FakeRedisModule:
        @staticmethod
        def from_url(url: str) -> object:
            return Closable()

    monkeypatch.setattr(cache, "Redis", FakeRedisModule)
    cache.get_client()

    await cache.close_client()

    assert closed == [True]
    assert cache.get_client.cache_info().currsize == 0


async def test_closing_an_unbuilt_client_is_a_no_op() -> None:
    """Shutdown after a startup that never answered a request must not build one."""
    cache.get_client.cache_clear()

    await cache.close_client()

    assert cache.get_client.cache_info().currsize == 0

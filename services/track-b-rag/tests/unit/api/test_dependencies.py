"""The Qdrant and Redis dependencies, which every other test replaces with fakes."""

from __future__ import annotations

import pytest

from track_b_rag.api.dependencies import get_qdrant, get_redis
from track_b_rag.vector_store import close_client


async def test_the_dependency_returns_the_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real body: one process-wide client, not a new one per request."""
    sentinel = object()
    monkeypatch.setattr("track_b_rag.api.dependencies.get_client", lambda: sentinel)

    assert await get_qdrant() is sentinel  # type: ignore[comparison-overlap]

    close_client()


async def test_the_redis_dependency_returns_the_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One process-wide client here too: a connection pool per request is not a pool."""
    sentinel = object()
    monkeypatch.setattr("track_b_rag.api.dependencies.get_cache_client", lambda: sentinel)

    assert await get_redis() is sentinel  # type: ignore[comparison-overlap]


async def test_the_consumer_dependency_reads_it_off_app_state() -> None:
    """The lifespan owns the consumer; the route asks the app it is running in."""
    from types import SimpleNamespace

    from redis.asyncio import Redis

    from track_b_rag.api.dependencies import get_transcript_consumer
    from track_b_rag.transcript_consumer import TranscriptConsumer

    consumer = TranscriptConsumer(Redis.from_url("redis://localhost:6379/0"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    request.app.state.transcript_consumer = consumer

    assert await get_transcript_consumer(request) is consumer  # type: ignore[arg-type]


async def test_no_consumer_on_app_state_is_none_not_an_error() -> None:
    """Most route tests build an app without the lifespan; that is a real state."""
    from types import SimpleNamespace

    from track_b_rag.api.dependencies import get_transcript_consumer

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    assert await get_transcript_consumer(request) is None  # type: ignore[arg-type]

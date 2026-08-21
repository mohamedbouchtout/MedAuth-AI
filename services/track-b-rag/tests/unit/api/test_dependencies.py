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

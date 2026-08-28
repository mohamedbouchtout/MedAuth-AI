"""Request-scoped dependencies: the database session and the Redis client.

Both are declared as FastAPI dependencies rather than reached for directly, so a
test can substitute a fake through ``app.dependency_overrides`` without a real
PostgreSQL or Redis in reach.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from track_a_clinical.config import get_settings
from track_a_clinical.consumer import TranscriptConsumer
from track_a_clinical.db import get_sessionmaker


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a session and roll back anything a failing handler left open.

    Handlers commit explicitly. The rollback here is the safety net for a handler
    that raised mid-transaction — without it the connection returns to the pool
    still inside a transaction.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@lru_cache(maxsize=1)
def _redis_client() -> Redis:
    """Return the process-wide Redis client, connected lazily on first command."""
    return Redis.from_url(get_settings().redis_url)


async def get_redis() -> Redis:
    """Return the Redis client used to publish session lifecycle signals."""
    return _redis_client()


async def close_redis() -> None:
    """Close the Redis client and forget it. Called on app shutdown."""
    if _redis_client.cache_info().currsize:
        await _redis_client().aclose()
    _redis_client.cache_clear()


async def get_transcript_consumer(request: Request) -> TranscriptConsumer | None:
    """Return the running transcript consumer, or None when there is none.

    The consumer is owned by the application lifespan and lives on
    ``app.state``, so it is reached through the request rather than a module
    global. An app built without the lifespan, as most route tests do, has no
    consumer, and ``GET /health`` reports that as ``error`` rather than raising.
    """
    consumer = getattr(request.app.state, "transcript_consumer", None)
    return consumer if isinstance(consumer, TranscriptConsumer) else None

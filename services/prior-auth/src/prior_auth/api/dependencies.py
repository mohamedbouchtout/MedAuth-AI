"""Request-scoped dependencies: the Redis client and the running consumer.

Declared as FastAPI dependencies rather than reached for directly, so a test can
substitute a fake through ``app.dependency_overrides`` without a real Redis or
database in reach. Mirrors track-a-clinical's module of the same name, including
its session dependency — TASK-061's submission router is this service's first
database-backed route. The assembly consumer still opens its own session through
the session factory, because it has no request to hang one off.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from prior_auth.config import get_settings
from prior_auth.consumer import SessionEndConsumer
from prior_auth.db import get_sessionmaker


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a session and roll back anything a failing handler left open.

    Handlers commit explicitly. The rollback here is the safety net for a handler
    that raised mid-transaction — without it the connection returns to the pool
    still inside a transaction. The same arrangement as track-a-clinical's.
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
    """Return the Redis client the session-end consumer subscribes through."""
    return _redis_client()


async def close_redis() -> None:
    """Close the Redis client and forget it. Called on app shutdown."""
    if _redis_client.cache_info().currsize:
        await _redis_client().aclose()
    _redis_client.cache_clear()


async def get_session_end_consumer(request: Request) -> SessionEndConsumer | None:
    """Return the running session-end consumer, or None when there is none.

    The consumer is owned by the application lifespan and lives on
    ``app.state``, so it is reached through the request rather than a module
    global. An app built without the lifespan, as most route tests do, has no
    consumer, and ``GET /health`` reports that as ``error`` rather than raising.
    """
    consumer = getattr(request.app.state, "session_end_consumer", None)
    return consumer if isinstance(consumer, SessionEndConsumer) else None

"""Request-scoped dependencies for track-b-rag: Qdrant, Redis and the database.

All three are reached through FastAPI dependencies rather than imported directly
at the call site, so a test can substitute a fake through
``app.dependency_overrides`` without a Qdrant, a Redis or a PostgreSQL in reach.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from qdrant_client import QdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from track_b_rag.cache import get_client as get_cache_client
from track_b_rag.db import get_sessionmaker
from track_b_rag.vector_store import get_client


async def get_qdrant() -> QdrantClient:
    """Return the process-wide Qdrant client."""
    return get_client()


async def get_redis() -> Redis:
    """Return the process-wide Redis client used to cache policy rules."""
    return get_cache_client()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a session and roll back anything a failing handler left open.

    Handlers commit explicitly. The rollback is the safety net for one that
    raised mid-transaction — without it the connection returns to the pool still
    inside a transaction.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

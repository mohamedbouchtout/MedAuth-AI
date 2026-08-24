"""Request-scoped dependencies for track-b-rag: Qdrant, Redis, the database and
the transcript consumer.

All of them are reached through FastAPI dependencies rather than imported
directly at the call site, so a test can substitute a fake through
``app.dependency_overrides`` without a Qdrant, a Redis or a PostgreSQL in reach.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from qdrant_client import QdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from track_b_rag.cache import get_client as get_cache_client
from track_b_rag.db import get_sessionmaker
from track_b_rag.transcript_consumer import TranscriptConsumer
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


async def get_transcript_consumer(request: Request) -> TranscriptConsumer | None:
    """Return the running transcript consumer, or None when there is none.

    The consumer is owned by the application lifespan and lives on
    ``app.state``, not in a module-level singleton: it holds a Redis pub/sub
    connection and a set of live sessions, and a process-wide instance that
    outlived its app would keep both. None is a real answer — an app built
    without the lifespan, as most route tests do, has no consumer, and
    ``GET /health`` reports that rather than pretending.
    """
    consumer = getattr(request.app.state, "transcript_consumer", None)
    return consumer if isinstance(consumer, TranscriptConsumer) else None

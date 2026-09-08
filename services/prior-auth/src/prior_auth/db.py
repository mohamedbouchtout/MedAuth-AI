"""Database wiring for prior-auth.

This service reads ``encounters``, ``clinical_notes`` and ``clinical_nudges``
and writes ``prior_auth_requests``, all through the mapped classes in
:mod:`track_a_clinical.models` — the single definition of that schema for the
whole monorepo (CLAUDE.md, "Where the shared SQLAlchemy models live"). No model
is declared here and no Alembic environment lives here: track-a-clinical owns
migration authorship, and a second mapping of the same table would drift from
the migration history with nothing to catch it.

Deliberately a near-copy of track-b-rag's module of the same name, which is the
established shape for a service that queries the shared schema without owning
it: no ``version_table``, no ``include_object``. Two spellings of one obligation
would be two things to keep right.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import cast

import asyncpg
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class DatabaseConfigurationError(RuntimeError):
    """Raised when no database URL is configured."""


def database_url() -> str:
    """Return ``DATABASE_URL`` as a SQLAlchemy asyncpg URL.

    CLAUDE.md specifies the ``postgresql+asyncpg://`` spelling, but a plain
    ``postgresql://`` is accepted too, so a value copied from psql or a
    container environment still works rather than failing with a driver error.
    One ``DATABASE_URL`` serves every consumer in the monorepo.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise DatabaseConfigurationError(
            "DATABASE_URL is not set — prior-auth needs a database URL to read "
            "an encounter's note and nudges and to record the bundle."
        )
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, created on first use.

    Lazy rather than built at import time: the unit suite imports this module
    without a database in reach, and the service must still start and report
    unhealthy when Postgres is slow to come up.
    """
    return create_async_engine(database_url(), pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the session factory bound to :func:`get_engine`.

    ``expire_on_commit=False`` because an expired attribute would be reloaded by
    a lazy SELECT on first access, and lazy IO raises in an async session.
    """
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def raw_asyncpg_connection(session: AsyncSession) -> asyncpg.Connection:
    """Return the asyncpg connection underlying an active SQLAlchemy session.

    This is what lets an audit write join the caller's transaction instead of
    running on hipaa-logger's own pool: the package takes an optional ``conn``
    for exactly this case (see CLAUDE.md, "hipaa-logger — Design Decisions").
    A bundle that exists with no audit row, and an audit row for a bundle that
    rolled back, are both worse than the write failing outright.

    Deliberately identical to the helpers of the same name in track-a-clinical
    and track-b-rag.
    """
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    return cast("asyncpg.Connection", raw.driver_connection)


async def dispose_engine() -> None:
    """Close the engine's connection pool and forget it. Called on app shutdown."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

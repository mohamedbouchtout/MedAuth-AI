"""Database wiring for track-b-rag.

This service writes one table, ``insurance_policies``, using the mapped class
from :mod:`track_a_clinical.models` — the single definition of that table for
the whole monorepo (CLAUDE.md, "Where the shared SQLAlchemy models live"). No
model is declared here and no Alembic environment lives here: track-a-clinical
owns migration authorship for the shared schema, and a second mapping of the
same table would drift from the migration history with nothing to catch it.

That is the whole difference between this module and track-a-clinical's ``db.py``,
which it otherwise mirrors: no ``version_table``, no ``include_object``, no
``raw_asyncpg_connection``. The last one is absent for a reason worth stating —
it exists there to put an audit write inside the request's transaction, and this
service's one route writes no audit row at all (Known Constraints #6: policy
documents are public payer publications, not PHI).
"""

from __future__ import annotations

import os
from functools import lru_cache

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
    Same normalisation track-a-clinical does — one ``DATABASE_URL`` serves every
    consumer in the monorepo.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise DatabaseConfigurationError(
            "DATABASE_URL is not set — track-b-rag needs a database URL to record "
            "ingested policies."
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


async def dispose_engine() -> None:
    """Close the engine's connection pool and forget it. Called on app shutdown."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

"""Database wiring shared by the service and its Alembic environment.

Kept out of ``migrations/env.py`` because importing that module runs migrations
as a side effect — anything the service or its tests also need has to live here
instead.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Final, cast

import asyncpg
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

#: Namespaced so this history cannot collide with packages/hipaa-logger's in the
#: same database. See "Alembic version table isolation" in CLAUDE.md.
VERSION_TABLE: Final = "alembic_version_track_a_clinical"

#: Tables owned by another migration history. Autogenerate must not offer to drop
#: audit_log or hipaa-logger's version table just because they are absent from
#: this service's metadata.
FOREIGN_TABLES: Final[frozenset[str]] = frozenset({"audit_log", "alembic_version_hipaa_logger"})


class DatabaseConfigurationError(RuntimeError):
    """Raised when no database URL is configured."""


def database_url() -> str:
    """Return ``DATABASE_URL`` as a SQLAlchemy asyncpg URL.

    CLAUDE.md specifies the ``postgresql+asyncpg://`` spelling, but a plain
    ``postgresql://`` is accepted too, so a value copied from psql or a container
    environment still works rather than failing with a driver error.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise DatabaseConfigurationError(
            "DATABASE_URL is not set — track-a-clinical needs a database URL."
        )
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    """Restrict Alembic autogenerate and comparison to this history's own tables."""
    return not (type_ == "table" and name in FOREIGN_TABLES)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, created on first use.

    Built lazily rather than at import time: ``migrations/env.py`` and the unit
    suite both import this module without a database in reach.
    """
    return create_async_engine(database_url(), pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the session factory bound to :func:`get_engine`.

    ``expire_on_commit=False`` because an expired attribute would be reloaded by
    a lazy SELECT on first access, and lazy IO raises in an async session. Route
    handlers refresh explicitly when they need a server-generated value back.
    """
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def dispose_engine() -> None:
    """Close the engine's connection pool and forget it. Called on app shutdown."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def raw_asyncpg_connection(session: AsyncSession) -> asyncpg.Connection:
    """Return the asyncpg connection underlying an active SQLAlchemy session.

    This is what lets an audit write join the caller's transaction instead of
    running on hipaa-logger's own pool: the package takes an optional ``conn``
    for exactly this case (see CLAUDE.md "hipaa-logger — Design Decisions"). An
    audit row written on a separate connection would survive a rollback of the
    change it claims to record, or be lost while that change commits.
    """
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    return cast("asyncpg.Connection", raw.driver_connection)

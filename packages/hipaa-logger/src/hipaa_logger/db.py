"""Connection management for the audit log.

The pool is created lazily on first use from ``DATABASE_URL`` and lives for the
process lifetime. Callers that need the audit write inside their own transaction
pass a connection to :func:`hipaa_logger.audit_log` instead; tests use
:func:`set_connection` to bypass the pool entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Final

import asyncpg

logger: Final = logging.getLogger(__name__)

_DEFAULT_MIN_POOL_SIZE: Final[int] = 1
_DEFAULT_MAX_POOL_SIZE: Final[int] = 10

# SQLAlchemy-style DSNs carry a driver suffix that asyncpg cannot parse. Services
# in this monorepo use SQLAlchemy and share one DATABASE_URL with this package, so
# both spellings have to work.
_DRIVER_SUFFIXES: Final[tuple[str, ...]] = ("+asyncpg", "+psycopg", "+psycopg2", "+pg8000")

_pool: asyncpg.Pool | None = None
_pool_lock: Final[asyncio.Lock] = asyncio.Lock()
_injected_connection: asyncpg.Connection | None = None
_dsn_override: str | None = None


class AuditLogError(RuntimeError):
    """Raised when an audit event cannot be recorded.

    Audit failures are never silent — HIPAA requires a durable record of every PHI
    access, so a failed write has to surface to the caller rather than be swallowed.
    """


class AuditConfigurationError(AuditLogError):
    """Raised when the audit logger has no usable database configuration."""


def normalize_dsn(url: str) -> str:
    """Strip any SQLAlchemy driver suffix from a PostgreSQL URL.

    ``postgresql+asyncpg://user@host/db`` becomes ``postgresql://user@host/db``.
    A URL that is already driver-free is returned unchanged.
    """
    scheme, separator, remainder = url.partition("://")
    if not separator:
        return url
    for suffix in _DRIVER_SUFFIXES:
        if scheme.endswith(suffix):
            scheme = scheme[: -len(suffix)]
            break
    return f"{scheme}://{remainder}"


def sqlalchemy_dsn(url: str) -> str:
    """Return `url` with the asyncpg driver suffix, as Alembic and SQLAlchemy expect."""
    normalized = normalize_dsn(url)
    scheme, separator, remainder = normalized.partition("://")
    if not separator:
        return normalized
    return f"{scheme}+asyncpg://{remainder}"


def configure(dsn: str | None) -> None:
    """Override the DSN used for the lazily created pool.

    Passing ``None`` clears the override and restores ``DATABASE_URL`` lookup.
    Has no effect on an already-created pool; call :func:`close_pool` first.
    """
    global _dsn_override
    _dsn_override = dsn


def resolve_dsn() -> str:
    """Return the normalized DSN, from the configured override or ``DATABASE_URL``."""
    raw = _dsn_override if _dsn_override is not None else os.environ.get("DATABASE_URL")
    if not raw:
        raise AuditConfigurationError(
            "No database configured for hipaa-logger: set DATABASE_URL, call configure(dsn), "
            "or pass conn= to audit_log()."
        )
    return normalize_dsn(raw)


def set_connection(conn: asyncpg.Connection | None) -> None:
    """Install a connection that every audit write uses, bypassing the pool.

    Intended for tests and for callers that hold a transaction open for the whole
    request. Passing ``None`` removes the injected connection.
    """
    global _injected_connection
    _injected_connection = conn


def get_injected_connection() -> asyncpg.Connection | None:
    """Return the connection installed by :func:`set_connection`, if any."""
    return _injected_connection


async def get_pool() -> asyncpg.Pool:
    """Return the shared pool, creating it on first use."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        # Another coroutine may have created the pool while this one waited.
        if _pool is None:
            dsn = resolve_dsn()
            try:
                _pool = await asyncpg.create_pool(
                    dsn,
                    min_size=_DEFAULT_MIN_POOL_SIZE,
                    max_size=_DEFAULT_MAX_POOL_SIZE,
                )
            except Exception as exc:  # noqa: BLE001 — re-raised as AuditLogError below
                raise AuditLogError(f"Could not connect to the audit database: {exc}") from exc
            logger.info("hipaa-logger connection pool created")
    assert _pool is not None  # noqa: S101 — narrows Optional for mypy after the lock
    return _pool


async def close_pool() -> None:
    """Close the shared pool. Call during application shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("hipaa-logger connection pool closed")

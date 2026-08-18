"""Database wiring shared by the service and its Alembic environment.

Kept out of ``migrations/env.py`` because importing that module runs migrations
as a side effect — anything the service or its tests also need has to live here
instead.
"""

from __future__ import annotations

import os
from typing import Final

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

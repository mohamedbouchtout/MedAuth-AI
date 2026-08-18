"""Declarative base and shared column conventions for the core schema.

Every table under this base is created by the Alembic history in
``services/track-a-clinical/migrations``. ``Base.metadata`` is what that
history's ``env.py`` compares against, so a model changed without a matching
revision shows up as autogenerate drift rather than passing silently.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB columns hold structures whose exact shape is set by the task that writes
# them, and each column documents its own. `Any` inside a JSON document is
# genuinely unavoidable, unlike a bare `Any` on a function signature.
JsonObject = dict[str, Any]

#: ``gen_random_uuid()`` is a PostgreSQL 13+ builtin — no pgcrypto extension needed.
#: IDs are generated server-side per the database conventions in CLAUDE.md.
UUID_SERVER_DEFAULT: Final = sa.text("gen_random_uuid()")

#: ``NOW()`` rather than a Python-side timestamp, so rows written by different
#: services on different hosts share one clock.
NOW_SERVER_DEFAULT: Final = sa.text("NOW()")


class Base(DeclarativeBase):
    """Declarative base for the schema track-a-clinical migrates."""


def uuid_primary_key() -> Mapped[uuid.UUID]:
    """Return the ``id UUID PRIMARY KEY DEFAULT gen_random_uuid()`` column."""
    return mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=UUID_SERVER_DEFAULT,
    )


def timestamp_column(*, nullable: bool, default_now: bool = False) -> Mapped[Any]:
    """Return a ``TIMESTAMPTZ`` column, optionally defaulting to ``NOW()``."""
    return mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=NOW_SERVER_DEFAULT if default_now else None,
    )


def soft_delete_column() -> Mapped[datetime.datetime | None]:
    """Return the ``deleted_at`` column.

    Rows are retired by setting this, never by ``DELETE`` — clinical records carry
    a retention obligation, and a hard delete would orphan the audit-trail rows
    that reference them.
    """
    return timestamp_column(nullable=True)

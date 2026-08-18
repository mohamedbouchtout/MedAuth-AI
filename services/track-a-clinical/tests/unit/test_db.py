"""Database URL resolution and the autogenerate filter."""

from __future__ import annotations

import pytest

from track_a_clinical.db import (
    FOREIGN_TABLES,
    VERSION_TABLE,
    DatabaseConfigurationError,
    database_url,
    include_object,
)


def test_asyncpg_url_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host/db")
    assert database_url() == "postgresql+asyncpg://u:p@host/db"


def test_plain_postgresql_url_gains_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL copied from psql or a container env still works."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    assert database_url() == "postgresql+asyncpg://u:p@host/db"


def test_only_the_scheme_is_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """A password or database name containing the scheme text is left alone."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:postgresql://@host/db")
    assert database_url() == "postgresql+asyncpg://u:postgresql://@host/db"


def test_missing_url_is_an_error_not_a_silent_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(DatabaseConfigurationError):
        database_url()


def test_version_table_is_namespaced() -> None:
    """Sharing alembic_version with hipaa-logger would corrupt both histories."""
    assert VERSION_TABLE == "alembic_version_track_a_clinical"


def test_foreign_tables_are_excluded_from_autogenerate() -> None:
    for name in FOREIGN_TABLES:
        assert include_object(None, name, "table", True, None) is False


def test_own_tables_and_non_table_objects_are_included() -> None:
    assert include_object(None, "encounters", "table", True, None) is True
    # A column named like a foreign table is still ours.
    assert include_object(None, "audit_log", "column", True, None) is True

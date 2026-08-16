"""Unit tests for connection configuration and DSN handling."""

from __future__ import annotations

import pytest

from hipaa_logger import configure, normalize_dsn, sqlalchemy_dsn
from hipaa_logger.db import AuditConfigurationError, resolve_dsn


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql+asyncpg://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgresql+psycopg://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgresql+psycopg2://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgres://u:p@h:5432/db", "postgres://u:p@h:5432/db"),
    ],
)
def test_normalize_dsn_strips_driver_suffix(raw: str, expected: str) -> None:
    assert normalize_dsn(raw) == expected


def test_normalize_dsn_leaves_non_url_untouched() -> None:
    assert normalize_dsn("not-a-url") == "not-a-url"


def test_normalize_dsn_preserves_query_parameters() -> None:
    raw = "postgresql+asyncpg://u:p@h/db?sslmode=require"
    assert normalize_dsn(raw) == "postgresql://u:p@h/db?sslmode=require"


@pytest.mark.parametrize(
    "raw",
    ["postgresql://u@h/db", "postgresql+asyncpg://u@h/db"],
)
def test_sqlalchemy_dsn_always_carries_the_asyncpg_driver(raw: str) -> None:
    assert sqlalchemy_dsn(raw) == "postgresql+asyncpg://u@h/db"


def test_resolve_dsn_prefers_the_configured_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://from-env/db")
    configure("postgresql+asyncpg://from-override/db")
    try:
        assert resolve_dsn() == "postgresql://from-override/db"
    finally:
        configure(None)


def test_resolve_dsn_falls_back_to_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(None)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://from-env/db")
    assert resolve_dsn() == "postgresql://from-env/db"


def test_resolve_dsn_raises_when_nothing_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(AuditConfigurationError, match="No database configured"):
        resolve_dsn()

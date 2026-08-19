"""DATABASE_URL handling and the lazily-built engine.

Nothing here connects. The engine is created but never used, which is the point:
the service has to import and start without a database in reach.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from track_b_rag.db import (
    DatabaseConfigurationError,
    database_url,
    dispose_engine,
    get_engine,
    get_sessionmaker,
)


@pytest.fixture(autouse=True)
def clean_engine() -> Iterator[None]:
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def test_the_sqlalchemy_spelling_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://medauth:pw@localhost/medauth")

    assert database_url() == "postgresql+asyncpg://medauth:pw@localhost/medauth"


def test_a_plain_postgresql_url_gains_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value copied from psql still works rather than failing with a driver error."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://medauth:pw@localhost/medauth")

    assert database_url() == "postgresql+asyncpg://medauth:pw@localhost/medauth"


def test_only_the_scheme_is_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/postgresql://weird")

    assert database_url() == "postgresql+asyncpg://u:p@host/postgresql://weird"


def test_a_missing_url_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(DatabaseConfigurationError, match="DATABASE_URL"):
        database_url()


def test_an_empty_url_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")

    with pytest.raises(DatabaseConfigurationError):
        database_url()


def test_the_engine_is_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://medauth:pw@localhost/medauth")

    assert get_engine() is get_engine()


def test_the_sessionmaker_is_bound_to_that_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://medauth:pw@localhost/medauth")

    assert get_sessionmaker().kw["bind"] is get_engine()


def test_sessions_do_not_expire_on_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired attribute would trigger lazy IO, which raises in an async session."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://medauth:pw@localhost/medauth")

    assert get_sessionmaker().kw["expire_on_commit"] is False


async def test_disposing_clears_both_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://medauth:pw@localhost/medauth")
    first = get_engine()
    get_sessionmaker()

    await dispose_engine()

    assert get_engine() is not first


async def test_disposing_an_unbuilt_engine_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown must not construct an engine just to close it."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    await dispose_engine()

    assert get_engine.cache_info().currsize == 0

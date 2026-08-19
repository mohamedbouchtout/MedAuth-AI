"""The engine, session factory and shutdown hooks added for the HTTP surface.

No database is touched: SQLAlchemy builds an engine without connecting, and a
session only opens a connection when it first runs a statement. These tests
cover the wiring itself — that it is built once, disposed cleanly, and that a
handler which raises leaves no session open behind it.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from track_a_clinical.api.dependencies import close_redis, get_db_session
from track_a_clinical.config import get_settings
from track_a_clinical.db import dispose_engine, get_engine, get_sessionmaker

FAKE_URL = "postgresql+asyncpg://medauth:unused@127.0.0.1:5432/unused"


@pytest.fixture(autouse=True)
async def _wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", FAKE_URL)
    monkeypatch.setenv("JWT_SIGNING_KEY", "db-wiring-test-key-padded-to-32b")
    get_settings.cache_clear()
    await dispose_engine()
    await close_redis()


async def test_engine_and_sessionmaker_are_built_once() -> None:
    """Both are cached: a second engine would mean a second connection pool."""
    assert get_engine() is get_engine()
    assert get_sessionmaker() is get_sessionmaker()
    assert get_sessionmaker().kw["bind"] is get_engine()


async def test_sessions_do_not_expire_on_commit() -> None:
    """Expired attributes would reload with a lazy SELECT, which raises in async."""
    assert get_sessionmaker().kw["expire_on_commit"] is False


async def test_dispose_engine_forgets_the_cached_engine() -> None:
    first = get_engine()

    await dispose_engine()

    assert get_engine() is not first


async def test_dispose_engine_is_safe_when_nothing_was_built() -> None:
    """App shutdown must not fail just because no request ever hit the database."""
    await dispose_engine()
    await dispose_engine()


async def test_dependency_yields_a_session() -> None:
    generator = get_db_session()
    session = await anext(generator)

    assert isinstance(session, AsyncSession)

    with pytest.raises(StopAsyncIteration):
        await anext(generator)


async def test_dependency_rolls_back_when_the_handler_raises() -> None:
    """Without the rollback the connection returns to the pool mid-transaction."""
    generator = get_db_session()
    session = await anext(generator)
    rolled_back = False

    async def record_rollback() -> None:
        nonlocal rolled_back
        rolled_back = True

    session.rollback = record_rollback  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="handler blew up"):
        await generator.athrow(RuntimeError("handler blew up"))

    assert rolled_back


async def test_close_redis_is_safe_when_no_client_was_built() -> None:
    await close_redis()
    await close_redis()

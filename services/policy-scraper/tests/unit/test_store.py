"""Reading what is already ingested — the URL handling, without a database.

The query itself needs Postgres and is covered in
``tests/integration/test_store.py``. What is worth pinning without one is that a
caller who set DATABASE_URL in either accepted form gets a working engine, since
that is a start-up failure rather than a query failure.
"""

from __future__ import annotations

from policy_scraper.store import known_content_hashes, session_factory


def test_a_sqlalchemy_style_url_is_used_as_is() -> None:
    """The form CI and .env.example set."""
    factory = session_factory("postgresql+asyncpg://user:pass@host/db")

    assert factory.kw["bind"].url.drivername == "postgresql+asyncpg"


def test_a_bare_postgresql_url_gets_the_async_driver() -> None:
    """A caller who copied a psql URL should not be told it is wrong — this is
    the same defensive rewrite hipaa-logger does in the other direction."""
    factory = session_factory("postgresql://user:pass@host/db")

    assert factory.kw["bind"].url.drivername == "postgresql+asyncpg"


def test_the_rewrite_only_touches_the_scheme() -> None:
    """A password containing the scheme text must survive intact."""
    factory = session_factory("postgresql://user:postgresql://@host/db")

    assert factory.kw["bind"].url.password == "postgresql://"


async def test_no_policy_ids_means_no_query() -> None:
    """A first run against an empty selection should not open a connection at
    all; passing None here would fail if the function tried."""
    assert await known_content_hashes(None, set()) == {}  # type: ignore[arg-type]

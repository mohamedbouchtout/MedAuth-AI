"""The migration history against a real PostgreSQL database.

Skipped when DATABASE_URL is unset, so the unit suite still runs on a machine
with no database. In CI the `test` job applies migrations before pytest and
these tests re-apply them — `alembic upgrade head` on a current database is a
no-op, so that is safe rather than redundant.

Reflection and autogenerate are synchronous APIs, and asyncpg is the only
PostgreSQL driver in this workspace, so they run through ``run_sync`` on an
async connection rather than pulling in psycopg just for the tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from track_a_clinical.db import VERSION_TABLE, database_url, include_object
from track_a_clinical.models import Base

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL is not set — migration tests need a real PostgreSQL",
    ),
]

SERVICE_ROOT = Path(__file__).resolve().parents[2]

CORE_TABLES = (
    "encounters",
    "clinical_notes",
    "clinical_nudges",
    "prior_auth_requests",
    "insurance_policies",
)

CORE_INDEXES = (
    "idx_encounters_session",
    "idx_encounters_provider",
    "idx_clinical_notes_encounter",
    "idx_clinical_nudges_encounter",
    "idx_prior_auth_encounter",
    "idx_prior_auth_status",
    "idx_insurance_policies_payer_state",
)


def run_alembic(*args: str) -> None:
    """Run an Alembic command in the service directory, failing loudly."""
    result = subprocess.run(  # noqa: S603 — fixed command, no user input
        [sys.executable, "-m", "alembic", *args],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


def table_names(connection: Connection) -> set[str]:
    """Reflect the current table list. Runs inside ``run_sync``."""
    return set(sa.inspect(connection).get_table_names())


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> Iterator[None]:
    """Bring the database to head before these tests, and leave it there after.

    The teardown matters: the downgrade test takes the database to base, and a
    failure part-way through would otherwise leave it empty for whatever runs next.
    """
    run_alembic("upgrade", "head")
    yield
    run_alembic("upgrade", "head")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test, so no connection caches a pre-DDL snapshot."""
    async_engine = create_async_engine(database_url())
    try:
        yield async_engine
    finally:
        await async_engine.dispose()


async def test_upgrade_creates_every_core_table(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        present = await connection.run_sync(table_names)
    assert set(CORE_TABLES) <= present


async def test_upgrade_creates_every_index(engine: AsyncEngine) -> None:
    def index_names(connection: Connection) -> set[str]:
        inspector = sa.inspect(connection)
        return {
            index["name"]
            for table in CORE_TABLES
            for index in inspector.get_indexes(table)
            if index["name"]
        }

    async with engine.connect() as connection:
        present = await connection.run_sync(index_names)
    assert set(CORE_INDEXES) <= present


async def test_version_table_is_namespaced_and_the_default_is_unused(
    engine: AsyncEngine,
) -> None:
    """Two histories share this database; neither may claim `alembic_version`."""
    async with engine.connect() as connection:
        namespaced = await connection.scalar(
            sa.text(f"SELECT to_regclass('public.{VERSION_TABLE}')")
        )
        default = await connection.scalar(sa.text("SELECT to_regclass('public.alembic_version')"))
    assert namespaced is not None
    assert default is None


async def test_models_match_the_migrated_database(engine: AsyncEngine) -> None:
    """The real drift guard: autogenerate sees nothing to change.

    A column added to a model without a revision — or a revision written without
    updating the model — fails here rather than surfacing as a runtime error in
    whichever service happens to write that table first.
    """

    def diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(
            connection,
            opts={"include_object": include_object, "version_table": VERSION_TABLE},
        )
        return list(compare_metadata(context, Base.metadata))

    async with engine.connect() as connection:
        differences = await connection.run_sync(diff)
    assert differences == []


async def test_downgrade_removes_the_core_tables_and_upgrade_restores_them(
    engine: AsyncEngine,
) -> None:
    """The history is reversible, and it only reverses what it owns."""
    async with engine.connect() as connection:
        before = await connection.run_sync(table_names)

    run_alembic("downgrade", "base")

    async with engine.connect() as connection:
        after_downgrade = await connection.run_sync(table_names)
    assert after_downgrade.isdisjoint(CORE_TABLES)
    # Everything this history does not own survives — most importantly
    # hipaa-logger's audit_log, which has its own history and version table.
    assert (before - set(CORE_TABLES) - {VERSION_TABLE}) <= after_downgrade

    run_alembic("upgrade", "head")

    async with engine.connect() as connection:
        after_upgrade = await connection.run_sync(table_names)
    assert set(CORE_TABLES) <= after_upgrade

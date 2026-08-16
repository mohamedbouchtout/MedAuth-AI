"""Integration test against a real PostgreSQL database.

Runs the package's own Alembic migration, writes an audit event through the real
pool, and reads the row back. Skipped when DATABASE_URL is unset so the unit suite
still runs on a machine with no database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from hipaa_logger import audit_log, close_pool, configure
from hipaa_logger.db import normalize_dsn

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL is not set — integration test needs a real PostgreSQL",
    ),
]

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    """Apply this package's migration before the tests run."""
    result = subprocess.run(  # noqa: S603 — fixed command, no user input
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture
async def connection() -> AsyncIterator[asyncpg.Connection]:
    """A direct connection used to read rows back, separate from the package pool."""
    conn = await asyncpg.connect(normalize_dsn(os.environ["DATABASE_URL"]))
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _reset_pool() -> AsyncIterator[None]:
    configure(None)
    yield
    await close_pool()


async def test_migration_creates_the_namespaced_version_table(
    connection: asyncpg.Connection,
) -> None:
    """The version table is namespaced, so other setups cannot collide with it."""
    exists = await connection.fetchval(
        "SELECT to_regclass('public.alembic_version_hipaa_logger') IS NOT NULL"
    )
    assert exists is True


async def test_audit_row_lands_with_every_column_populated(
    connection: asyncpg.Connection,
) -> None:
    actor_id = uuid.uuid4()
    session_id = uuid.uuid4()
    request_id = uuid.uuid4()
    resource_id = f"patient-{uuid.uuid4()}"

    await audit_log(
        actor_id=str(actor_id),
        action="READ_PATIENT",
        resource_type="Patient",
        resource_id=resource_id,
        session_id=str(session_id),
        service_name="track-b-rag",
        request_id=str(request_id),
        ip_address="198.51.100.7",
        user_agent="MedAuth/1.0 (integration test)",
    )

    row = await connection.fetchrow(
        "SELECT * FROM audit_log WHERE resource_id = $1",
        resource_id,
    )

    assert row is not None
    assert row["actor_id"] == actor_id
    assert row["action"] == "READ_PATIENT"
    assert row["resource_type"] == "Patient"
    assert row["session_id"] == session_id
    assert row["service_name"] == "track-b-rag"
    assert row["request_id"] == request_id
    assert str(row["ip_address"]) == "198.51.100.7"
    assert row["user_agent"] == "MedAuth/1.0 (integration test)"
    assert row["occurred_at"] is not None


async def test_nullable_columns_accept_none(connection: asyncpg.Connection) -> None:
    resource_id = f"encounter-{uuid.uuid4()}"

    await audit_log(
        actor_id=None,
        action="SYSTEM_SWEEP",
        resource_type=None,
        resource_id=resource_id,
        session_id=None,
        service_name="policy-scraper",
    )

    row = await connection.fetchrow(
        "SELECT * FROM audit_log WHERE resource_id = $1",
        resource_id,
    )

    assert row is not None
    assert row["actor_id"] is None
    assert row["session_id"] is None
    assert row["request_id"] is None
    assert row["ip_address"] is None
    assert row["user_agent"] is None


async def test_caller_transaction_rollback_discards_the_audit_row(
    connection: asyncpg.Connection,
) -> None:
    """A caller passing its own connection controls the transaction boundary."""
    resource_id = f"note-{uuid.uuid4()}"

    transaction = connection.transaction()
    await transaction.start()
    await audit_log(
        actor_id=None,
        action="WRITE_NOTE",
        resource_type="ClinicalNote",
        resource_id=resource_id,
        session_id=None,
        service_name="track-a-clinical",
        conn=connection,
    )
    await transaction.rollback()

    row = await connection.fetchrow(
        "SELECT id FROM audit_log WHERE resource_id = $1",
        resource_id,
    )
    assert row is None

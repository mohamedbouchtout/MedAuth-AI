"""Smoke test for scripts/seed-test-encounters.py.

Checks the two things that matter about a seed script: it produces the rows it
claims to, and running it twice does not double them. Patient identifiers are
placeholders until TASK-052 loads Synthea patients, so nothing here asserts on
their values beyond their presence.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from track_a_clinical.db import database_url

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL is not set — the seed script needs a real PostgreSQL",
    ),
]

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVICE_ROOT.parents[1]
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed-test-encounters.py"

EXPECTED_ROWS = 5

#: Every seeded row carries this prefix, which is what makes the fixture able to
#: clean up after itself without touching anything else in the table.
SEEDED_ROWS = sa.text("SELECT * FROM encounters WHERE ehr_encounter_id LIKE 'ehr-encounter-%'")
DELETE_SEEDED_ROWS = sa.text("DELETE FROM encounters WHERE ehr_encounter_id LIKE 'ehr-encounter-%'")


def run_seed(*, with_database_url: bool = True) -> subprocess.CompletedProcess[str]:
    """Run the seed script the way a developer would, from the repository root."""
    env = dict(os.environ)
    if not with_database_url:
        env.pop("DATABASE_URL", None)
    return subprocess.run(  # noqa: S603 — fixed command, no user input
        [sys.executable, str(SEED_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> Iterator[None]:
    """The seed script inserts rows; it does not create tables."""
    result = subprocess.run(  # noqa: S603 — fixed command, no user input
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")
    yield


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    async_engine = create_async_engine(database_url())
    try:
        yield async_engine
    finally:
        await async_engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_seed_rows(engine: AsyncEngine) -> AsyncIterator[None]:
    """Start and finish with no seeded rows, so the counts assert exactly."""
    async with engine.begin() as connection:
        await connection.execute(DELETE_SEEDED_ROWS)
    yield
    async with engine.begin() as connection:
        await connection.execute(DELETE_SEEDED_ROWS)


async def seeded_rows(engine: AsyncEngine) -> list[sa.Row[tuple[object, ...]]]:
    """Return the seeded encounters, read on a fresh connection."""
    async with engine.connect() as connection:
        result = await connection.execute(SEEDED_ROWS)
        return list(result)


async def test_seed_inserts_five_encounters(engine: AsyncEngine) -> None:
    completed = run_seed()
    assert completed.returncode == 0, completed.stderr

    rows = await seeded_rows(engine)
    assert len(rows) == EXPECTED_ROWS
    assert all(row.patient_fhir_id for row in rows)
    assert all(row.insurance_payer for row in rows)
    assert {row.status for row in rows} == {"active"}
    # Distinct payers, so a later RAG cache keyed on payer has something to
    # distinguish these encounters by.
    assert len({row.insurance_payer for row in rows}) == EXPECTED_ROWS


async def test_seed_is_idempotent(engine: AsyncEngine) -> None:
    """A second run inserts nothing — the ids are derived, and session_id is unique."""
    assert run_seed().returncode == 0
    first = {row.session_id for row in await seeded_rows(engine)}

    assert run_seed().returncode == 0
    second = {row.session_id for row in await seeded_rows(engine)}

    assert len(second) == EXPECTED_ROWS
    assert first == second


def test_seed_fails_clearly_without_a_database_url() -> None:
    """A missing DATABASE_URL is a message and a non-zero exit, not a traceback."""
    completed = run_seed(with_database_url=False)
    assert completed.returncode == 1
    assert "DATABASE_URL" in completed.stdout + completed.stderr
    assert "Traceback" not in completed.stderr

"""The pre-upload digest lookup, against a real database.

Marked integration because it needs the Postgres from docker-compose with both
migration histories applied — the same stack CI brings up.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from policy_scraper.store import known_content_hashes, session_factory
from track_a_clinical.models import InsurancePolicy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="needs the Postgres from docker-compose (DATABASE_URL unset)",
    ),
]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session whose rows are removed afterwards, whatever the test did."""
    factory = session_factory(os.environ["DATABASE_URL"])
    async with factory() as db:
        yield db
        await db.rollback()


@pytest_asyncio.fixture
async def policy(session: AsyncSession) -> AsyncIterator[InsurancePolicy]:
    row = InsurancePolicy(
        payer="cms-medicare",
        policy_id=f"cms-lcd-TEST{uuid.uuid4().hex[:8]}",
        content_hash="a" * 64,
        qdrant_collection="insurance_policies",
        jurisdiction_states=["MA", "NY"],
    )
    session.add(row)
    await session.commit()
    yield row
    await session.execute(
        sa.delete(InsurancePolicy).where(InsurancePolicy.policy_id == row.policy_id)
    )
    await session.commit()


async def test_a_recorded_policy_returns_its_digest(
    session: AsyncSession, policy: InsurancePolicy
) -> None:
    known = await known_content_hashes(session, {policy.policy_id})

    assert known == {policy.policy_id: "a" * 64}


async def test_an_unrecorded_policy_is_simply_absent(
    session: AsyncSession, policy: InsurancePolicy
) -> None:
    """A first sighting has no digest, so the scraper uploads it — no row, no
    special case."""
    known = await known_content_hashes(session, {policy.policy_id, "cms-lcd-NEVER-SEEN"})

    assert "cms-lcd-NEVER-SEEN" not in known
    assert policy.policy_id in known


async def test_one_query_covers_every_policy_id(
    session: AsyncSession, policy: InsurancePolicy
) -> None:
    """A few dozen round trips to save a few dozen uploads would be a poor
    trade, so the lookup is a single IN query."""
    known = await known_content_hashes(
        session, {policy.policy_id, *(f"cms-lcd-ABSENT{n}" for n in range(50))}
    )

    assert list(known) == [policy.policy_id]


async def test_the_jurisdiction_survives_a_round_trip(
    session: AsyncSession, policy: InsurancePolicy
) -> None:
    """The column TASK-013 added, read back as a list rather than as a string."""
    stored = await session.scalar(
        sa.select(InsurancePolicy).where(InsurancePolicy.policy_id == policy.policy_id)
    )

    assert stored is not None
    assert stored.jurisdiction_states == ["MA", "NY"]

"""PATCH /nudges/{nudge_id}/acknowledge against a real database.

What a fake cannot prove, and what these tests are for:

* **The join actually hides a soft-deleted encounter's nudges.** ``clinical_nudges``
  has no ``deleted_at`` of its own, so this is a property of the statement
  meeting real rows, not of the route's control flow. The unit suite asserts
  the join is in the SQL; this asserts what the SQL does.
* **The repeat is idempotent in the database, not just in the response.** The
  route decides from the row it read, and the row it reads the second time is
  the one the first call committed.
* **The audit row joins the acknowledgement's transaction**, and carries the
  encounter's provider rather than anything a caller sent. Two rows for two
  calls, and the second names a read.

Skipped when DATABASE_URL is unset, so the unit suite still runs on a machine
with nothing up. Each test writes its own encounter and removes it afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from track_a_clinical.models import ClinicalNudge, Encounter
from track_b_rag.api.dependencies import get_db_session
from track_b_rag.main import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="needs a real PostgreSQL (DATABASE_URL)",
    ),
]


def database_url() -> str:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url())
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """The test's own session, separate from the one the route commits on."""
    async with sessionmaker() as db_session:
        yield db_session


@pytest_asyncio.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """The app, with the route's session bound to this test's engine.

    A session per request, as the real dependency does — the route commits, and
    a shared session would let a later assertion see uncommitted state.
    """

    async def db_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as request_session:
            yield request_session

    app = create_app()
    app.dependency_overrides[get_db_session] = db_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://track-b-rag") as http:
        yield http


@pytest_asyncio.fixture
async def encounter(session: AsyncSession) -> AsyncIterator[Encounter]:
    """A real encounter, with its nudges, removed afterwards."""
    row = Encounter(
        session_id=uuid.uuid4(),
        patient_fhir_id="Patient/synthetic-task-041b",
        provider_id=uuid.uuid4(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    yield row
    await session.execute(sa.delete(ClinicalNudge).where(ClinicalNudge.encounter_id == row.id))
    await session.execute(sa.delete(Encounter).where(Encounter.id == row.id))
    await session.commit()


@pytest_asyncio.fixture
async def nudge(session: AsyncSession, encounter: Encounter) -> ClinicalNudge:
    """One unacknowledged nudge, as TASK-040's emitter would have written it."""
    row = ClinicalNudge(
        encounter_id=encounter.id,
        procedure_name="knee MRI",
        cpt_code="73721",
        nudge_message="Prior authorization required for knee MRI.",
        missing_criteria=["Failed six weeks of conservative therapy"],
        denial_risk="high",
        payer_policy_source="L33575",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def audit_rows(session: AsyncSession, nudge_id: uuid.UUID) -> list[Any]:
    result = await session.execute(
        sa.text(
            "SELECT actor_id, action, resource_type, resource_id, session_id, "
            "service_name, ip_address, user_agent FROM audit_log "
            "WHERE resource_id = :resource_id "
            "AND action IN ('ACKNOWLEDGE_NUDGE', 'READ_NUDGE') "
            "ORDER BY id"
        ),
        {"resource_id": str(nudge_id)},
    )
    return list(result.all())


async def test_an_acknowledgement_updates_the_row(
    client: AsyncClient, session: AsyncSession, nudge: ClinicalNudge
) -> None:
    response = await client.patch(f"/nudges/{nudge.id}/acknowledge", json={"acknowledged": True})

    assert response.status_code == 200
    await session.refresh(nudge)
    assert nudge.acknowledged is True
    assert nudge.acknowledged_at is not None
    assert response.json()["data"]["already_acknowledged"] is False


async def test_a_repeat_acknowledgement_does_not_move_the_timestamp(
    client: AsyncClient, session: AsyncSession, nudge: ClinicalNudge
) -> None:
    """A double tap records when the provider saw the alert, not when they tapped again."""
    first = await client.patch(f"/nudges/{nudge.id}/acknowledge", json={"acknowledged": True})
    second = await client.patch(f"/nudges/{nudge.id}/acknowledge", json={"acknowledged": True})

    assert second.status_code == 200
    assert second.json()["data"]["already_acknowledged"] is True
    assert second.json()["data"]["acknowledged_at"] == first.json()["data"]["acknowledged_at"]

    await session.refresh(nudge)
    assert nudge.acknowledged_at is not None


async def test_the_two_calls_audit_as_a_change_then_a_read(
    client: AsyncClient, session: AsyncSession, nudge: ClinicalNudge
) -> None:
    """The distinction TASK-006's idempotent session end already makes."""
    await client.patch(f"/nudges/{nudge.id}/acknowledge", json={"acknowledged": True})
    await client.patch(f"/nudges/{nudge.id}/acknowledge", json={"acknowledged": True})

    rows = await audit_rows(session, nudge.id)
    assert [row.action for row in rows] == ["ACKNOWLEDGE_NUDGE", "READ_NUDGE"]


async def test_the_audit_row_names_the_encounters_provider(
    client: AsyncClient, session: AsyncSession, encounter: Encounter, nudge: ClinicalNudge
) -> None:
    """The route carries no credential, so the encounter is the only actor there is."""
    await client.patch(f"/nudges/{nudge.id}/acknowledge", json={"acknowledged": True})

    (row,) = await audit_rows(session, nudge.id)
    assert row.actor_id == encounter.provider_id
    assert row.session_id == encounter.session_id
    assert row.resource_type == "ClinicalNudge"
    assert row.resource_id == str(nudge.id)
    assert row.service_name == "track-b-rag"


async def test_an_unknown_nudge_is_a_404(client: AsyncClient) -> None:
    response = await client.patch(
        f"/nudges/{uuid.uuid4()}/acknowledge", json={"acknowledged": True}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "nudge_not_found"


async def test_a_soft_deleted_encounters_nudge_is_a_404(
    client: AsyncClient, session: AsyncSession, encounter: Encounter, nudge: ClinicalNudge
) -> None:
    """The case the join exists for, and the one a nudge-row-only lookup would miss.

    The nudge row is untouched by the soft delete — it has no ``deleted_at`` and
    is never retired — so nothing but the join to ``encounters`` can tell that
    the visit behind it is gone.
    """
    encounter.deleted_at = sa.func.now()
    await session.commit()

    response = await client.patch(f"/nudges/{nudge.id}/acknowledge", json={"acknowledged": True})

    assert response.status_code == 404
    await session.refresh(nudge)
    assert nudge.acknowledged is False
    assert await audit_rows(session, nudge.id) == []

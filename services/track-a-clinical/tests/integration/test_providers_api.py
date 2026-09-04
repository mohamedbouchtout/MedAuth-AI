"""``POST /providers/resolve`` against real PostgreSQL.

Skipped when DATABASE_URL is unset, like every integration suite here, so the
unit tests still run on a machine with no backing services.

What only this suite can prove: that the ``UNIQUE`` constraint and
``ON CONFLICT DO NOTHING`` really behave the way the handler assumes. The unit
test's fake was written to match that assumption, so it cannot falsify it —
including the case that matters most, two concurrent launches by one clinician
racing to register the same practitioner.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from track_a_clinical.api.dependencies import get_db_session
from track_a_clinical.config import get_settings
from track_a_clinical.db import database_url
from track_a_clinical.main import create_app
from track_a_clinical.models import Provider

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL is not set — provider registry tests need a real PostgreSQL",
    ),
]

SIGNING_KEY = "integration-signing-key-padded-32"


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url())
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    async def override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://track-a-clinical") as http:
        yield http


def unique_reference() -> str:
    """A reference no other test run has registered.

    The table has no per-test isolation and a practitioner is registered
    permanently, so a fixed value would pass once and then exercise only the
    already-present path.
    """
    return f"https://ehr.example.com/fhir/Practitioner/{uuid.uuid4()}"


async def count_rows(sessions: async_sessionmaker[AsyncSession], reference: str) -> int:
    async with sessions() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(Provider)
                .where(Provider.fhir_practitioner_ref == reference)
            )
            or 0
        )


@pytest.mark.asyncio
async def test_it_writes_one_row_the_database_generated_the_id_for(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    reference = unique_reference()

    response = await client.post("/providers/resolve", json={"fhir_practitioner_ref": reference})

    assert response.status_code == 200
    provider_id = uuid.UUID(response.json()["data"]["provider_id"])
    assert await count_rows(sessions, reference) == 1

    async with sessions() as session:
        stored = await session.scalar(
            sa.select(Provider).where(Provider.fhir_practitioner_ref == reference)
        )
    assert stored is not None
    # Server-side gen_random_uuid(), per the UUID convention in CLAUDE.md.
    assert stored.id == provider_id
    assert stored.created_at is not None
    assert stored.deleted_at is None


@pytest.mark.asyncio
async def test_a_repeated_reference_reads_the_existing_row(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The path the unit fake models: the insert returns nothing, the select answers."""
    reference = unique_reference()
    body = {"fhir_practitioner_ref": reference}

    first = await client.post("/providers/resolve", json=body)
    second = await client.post("/providers/resolve", json=body)

    assert first.json()["data"]["provider_id"] == second.json()["data"]["provider_id"]
    assert await count_rows(sessions, reference) == 1


@pytest.mark.asyncio
async def test_concurrent_resolutions_agree_on_one_provider(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Two launches by one clinician at once must not mint two providers.

    This is the case the constraint exists for and the one a check-then-insert in
    Python would get wrong. A second row would split that clinician's encounters
    across two provider ids, and nothing anywhere would error.
    """
    reference = unique_reference()
    body = {"fhir_practitioner_ref": reference}

    responses = await asyncio.gather(
        *(client.post("/providers/resolve", json=body) for _ in range(8))
    )

    assert {response.status_code for response in responses} == {200}
    resolved = {response.json()["data"]["provider_id"] for response in responses}
    assert len(resolved) == 1
    assert await count_rows(sessions, reference) == 1

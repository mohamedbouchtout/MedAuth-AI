"""The note review routes against a real PostgreSQL and a real audit_log table.

The unit suite covers the request/response contract with fakes. What only a real
database can show is the part that matters most here: that a partial ``PATCH``
leaves untouched JSONB columns exactly as they were — including the difference
between NULL and ``[]``, which no in-memory object can prove survives a round
trip through the column — and that each access lands one row in ``audit_log``
on the request's own transaction.

Skipped when DATABASE_URL is unset, like the rest of this suite.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hipaa_logger import AuditAction, close_pool, configure
from track_a_clinical.api.dependencies import get_db_session
from track_a_clinical.db import database_url
from track_a_clinical.main import create_app
from track_a_clinical.models import (
    ENCOUNTER_STATUS_COMPLETED,
    SOURCE_PROVIDER_ACCEPTED,
    ClinicalNote,
    Encounter,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL is not set — the note routes need a real PostgreSQL",
    ),
]

PATIENT_FHIR_ID = "synthea-placeholder-1"

LLM_CODE = {
    "code": "M17.11",
    "display": "Unilateral primary osteoarthritis, right knee",
    "source": "llm-extraction",
    "confidence": None,
    "validation": None,
}
SUGGESTED_CODE = {
    "code": "E11.9",
    "display": "Type 2 diabetes mellitus without complications",
    "source": "comprehend-medical",
    "confidence": 0.91,
    "validation": None,
}


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url())
    configure(os.environ["DATABASE_URL"])
    yield async_sessionmaker(engine, expire_on_commit=False)
    await close_pool()
    await engine.dispose()


@pytest_asyncio.fixture
async def encounter(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Encounter]:
    """A completed encounter — the state a note is reviewed in."""
    row = Encounter(
        session_id=uuid.uuid4(),
        patient_fhir_id=PATIENT_FHIR_ID,
        provider_id=uuid.uuid4(),
        status=ENCOUNTER_STATUS_COMPLETED,
    )
    async with sessions() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)

    yield row

    async with sessions() as session:
        await session.execute(sa.delete(ClinicalNote).where(ClinicalNote.encounter_id == row.id))
        await session.execute(sa.delete(Encounter).where(Encounter.id == row.id))
        await session.execute(
            sa.text("DELETE FROM audit_log WHERE session_id = :sid"), {"sid": row.session_id}
        )
        await session.commit()


async def store_note(
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
    *,
    icd10_codes: list[dict[str, object]] | None,
    cpt_codes: list[dict[str, object]] | None,
) -> ClinicalNote:
    """Insert a note directly, standing in for what TASK-030's consumer wrote."""
    note = ClinicalNote(
        encounter_id=encounter.id,
        soap_subjective="Patient reports right knee pain for three months.",
        soap_objective="Tenderness over the medial joint line.",
        soap_assessment="Likely primary osteoarthritis of the right knee.",
        soap_plan="Order MRI right knee. Trial of physical therapy.",
        icd10_codes=icd10_codes,
        cpt_codes=cpt_codes,
    )
    async with sessions() as session:
        session.add(note)
        await session.commit()
        await session.refresh(note)
    return note


async def reload(sessions: async_sessionmaker[AsyncSession], note_id: uuid.UUID) -> ClinicalNote:
    """Read the row back from the database rather than trusting the response."""
    async with sessions() as session:
        row = await session.get(ClinicalNote, note_id)
        assert row is not None
        return row


async def audit_actions(
    sessions: async_sessionmaker[AsyncSession], session_id: uuid.UUID
) -> list[str]:
    statement = sa.text(
        "SELECT action FROM audit_log WHERE session_id = :sid ORDER BY occurred_at, id"
    )
    async with sessions() as session:
        return [row[0] for row in (await session.execute(statement, {"sid": session_id})).all()]


@pytest_asyncio.fixture
async def client(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """A client on the real app, with the real session dependency pointed at the test engine."""

    async def db_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db_session] = db_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://track-a-clinical"
    ) as http:
        yield http


async def test_a_stored_note_is_served_back(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    note = await store_note(sessions, encounter, icd10_codes=[LLM_CODE], cpt_codes=[])

    response = await client.get(f"/notes/{encounter.session_id}")

    assert response.status_code == 200
    assert response.json()["data"]["note_id"] == str(note.id)
    assert response.json()["data"]["icd10_codes"] == [LLM_CODE]
    assert response.json()["data"]["cpt_codes"] == []


async def test_a_read_writes_one_audit_row(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    await store_note(sessions, encounter, icd10_codes=[LLM_CODE], cpt_codes=[])

    await client.get(f"/notes/{encounter.session_id}")

    assert await audit_actions(sessions, encounter.session_id) == [AuditAction.READ_NOTE]


async def test_a_text_edit_leaves_an_undetermined_code_column_null(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    """The tri-state, proved through the column rather than through an object.

    ``icd10_codes`` starts NULL — the extraction pass never answered — and a
    ``PATCH`` that does not mention it must leave it that way. Writing ``[]``
    here would turn "not determined" into "determined to be none", which reads
    to a provider as an encounter with no diagnoses.
    """
    note = await store_note(sessions, encounter, icd10_codes=None, cpt_codes=[LLM_CODE])

    response = await client.patch(
        f"/notes/{encounter.session_id}", json={"soap_plan": "Order MRI right knee only."}
    )

    assert response.status_code == 200
    stored = await reload(sessions, note.id)
    assert stored.icd10_codes is None
    assert stored.cpt_codes == [LLM_CODE]
    assert stored.soap_plan == "Order MRI right knee only."
    assert stored.provider_edited is True


async def test_an_explicit_null_clears_the_column(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    note = await store_note(sessions, encounter, icd10_codes=[LLM_CODE], cpt_codes=[])

    response = await client.patch(f"/notes/{encounter.session_id}", json={"icd10_codes": None})

    assert response.status_code == 200
    assert (await reload(sessions, note.id)).icd10_codes is None


async def test_marking_reviewed_does_not_mark_edited(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    note = await store_note(sessions, encounter, icd10_codes=[LLM_CODE], cpt_codes=[])

    await client.patch(f"/notes/{encounter.session_id}", json={"reviewed_by_provider": True})

    stored = await reload(sessions, note.id)
    assert stored.reviewed_by_provider is True
    assert stored.provider_edited is False


async def test_accepting_a_suggestion_is_stored_as_documentation(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    """What makes a machine suggestion claimable by TASK-060."""
    note = await store_note(
        sessions, encounter, icd10_codes=[LLM_CODE, SUGGESTED_CODE], cpt_codes=[]
    )
    accepted = dict(SUGGESTED_CODE, source=SOURCE_PROVIDER_ACCEPTED, confidence=None)

    response = await client.patch(
        f"/notes/{encounter.session_id}", json={"icd10_codes": [LLM_CODE, accepted]}
    )

    assert response.status_code == 200
    stored = await reload(sessions, note.id)
    assert stored.icd10_codes == [LLM_CODE, accepted]


async def test_an_edit_writes_one_audit_row(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    await store_note(sessions, encounter, icd10_codes=[LLM_CODE], cpt_codes=[])

    await client.patch(f"/notes/{encounter.session_id}", json={"soap_plan": "Revised."})

    assert await audit_actions(sessions, encounter.session_id) == [AuditAction.UPDATE_NOTE]


async def test_an_encounter_without_a_note_is_distinguishable_from_an_unknown_one(
    client: AsyncClient, encounter: Encounter
) -> None:
    existing = await client.get(f"/notes/{encounter.session_id}")
    unknown = await client.get(f"/notes/{uuid.uuid4()}")

    assert existing.status_code == unknown.status_code == 404
    assert existing.json()["error"]["code"] == "note_not_generated"
    assert unknown.json()["error"]["code"] == "session_not_found"

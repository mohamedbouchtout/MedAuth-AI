"""The note writer against a real PostgreSQL, where the constraint actually exists.

The idempotency this file proves cannot be tested against a fake: it is the
database's own ``uq_clinical_notes_encounter`` doing the work, and
``ON CONFLICT DO NOTHING`` is a property of the statement PostgreSQL executes
rather than of anything in Python.

Skipped when DATABASE_URL is unset, like the rest of this suite.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hipaa_logger import close_pool, configure
from track_a_clinical import audit, notes
from track_a_clinical.db import database_url
from track_a_clinical.models import (
    ENCOUNTER_STATUS_ACTIVE,
    ClinicalNote,
    Encounter,
    ExtractedCode,
    load_codes,
)
from track_a_clinical.soap import GeneratedNote, SoapSections

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL is not set — the note writer needs a real PostgreSQL",
    ),
]

PATIENT_FHIR_ID = "synthea-placeholder-1"

FIRST = GeneratedNote(
    sections=SoapSections(
        subjective="First subjective.",
        objective="First objective.",
        assessment="First assessment.",
        plan="First plan.",
    ),
    icd10_codes=[ExtractedCode.from_llm("M17.11", "Osteoarthritis, right knee")],
    cpt_codes=[ExtractedCode.from_llm("73721")],
)

SECOND = GeneratedNote(
    sections=SoapSections(
        subjective="Second subjective.",
        objective="Second objective.",
        assessment="Second assessment.",
        plan="Second plan.",
    ),
    icd10_codes=[],
    cpt_codes=[],
)


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
    row = Encounter(
        session_id=uuid.uuid4(),
        patient_fhir_id=PATIENT_FHIR_ID,
        provider_id=uuid.uuid4(),
        status=ENCOUNTER_STATUS_ACTIVE,
    )
    async with sessions() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)

    yield row

    async with sessions() as session:
        await session.execute(sa.delete(ClinicalNote).where(ClinicalNote.encounter_id == row.id))
        await session.execute(sa.delete(Encounter).where(Encounter.id == row.id))
        await session.commit()


async def notes_for(
    sessions: async_sessionmaker[AsyncSession], encounter_id: uuid.UUID
) -> list[ClinicalNote]:
    async with sessions() as session:
        result = await session.scalars(
            sa.select(ClinicalNote).where(ClinicalNote.encounter_id == encounter_id)
        )
        return list(result)


async def count_audit_rows(
    sessions: async_sessionmaker[AsyncSession], session_id: uuid.UUID
) -> int:
    statement = sa.text(
        "SELECT count(*) FROM audit_log WHERE session_id = :sid AND action = :action"
    )
    async with sessions() as session:
        return int(
            await session.scalar(statement, {"sid": session_id, "action": audit.ACTION_WRITE_NOTE})
            or 0
        )


async def test_a_note_is_stored_with_its_codes(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    async with sessions() as session:
        note_id = await notes.store_note(session, encounter=encounter, note=FIRST)

    assert note_id is not None
    stored = await notes_for(sessions, encounter.id)
    assert len(stored) == 1
    assert stored[0].soap_plan == "First plan."
    assert [entry.code for entry in load_codes(stored[0].icd10_codes)] == ["M17.11"]
    assert [entry.code for entry in load_codes(stored[0].cpt_codes)] == ["73721"]


async def test_a_second_write_is_discarded_not_an_error(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    """A redelivered signal is an ordinary retry, not an integrity failure."""
    async with sessions() as session:
        await notes.store_note(session, encounter=encounter, note=FIRST)
    async with sessions() as session:
        second = await notes.store_note(session, encounter=encounter, note=SECOND)

    assert second is None
    assert len(await notes_for(sessions, encounter.id)) == 1


async def test_the_first_note_is_the_one_kept(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    """DO NOTHING, not DO UPDATE: a retry knows nothing the first attempt did not,
    and TASK-032's provider edits must not be replaced by a late duplicate."""
    async with sessions() as session:
        await notes.store_note(session, encounter=encounter, note=FIRST)
    async with sessions() as session:
        await notes.store_note(session, encounter=encounter, note=SECOND)

    stored = await notes_for(sessions, encounter.id)
    assert stored[0].soap_plan == "First plan."


async def test_a_discarded_write_audits_nothing(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    """An audit row claiming a PHI write that did not happen is worse than none."""
    async with sessions() as session:
        await notes.store_note(session, encounter=encounter, note=FIRST)
    async with sessions() as session:
        await notes.store_note(session, encounter=encounter, note=SECOND)

    assert await count_audit_rows(sessions, encounter.session_id) == 1


async def test_unset_codes_are_stored_as_null(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    """The extraction pass failed — different from it finding nothing."""
    note = GeneratedNote(sections=FIRST.sections, icd10_codes=None, cpt_codes=None)

    async with sessions() as session:
        await notes.store_note(session, encounter=encounter, note=note)

    stored = await notes_for(sessions, encounter.id)
    assert stored[0].icd10_codes is None
    assert stored[0].cpt_codes is None


async def test_codes_that_were_looked_for_and_absent_are_stored_as_empty(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    async with sessions() as session:
        await notes.store_note(session, encounter=encounter, note=SECOND)

    stored = await notes_for(sessions, encounter.id)
    assert stored[0].icd10_codes == []
    assert stored[0].cpt_codes == []


async def test_the_encounter_is_found_by_its_session_id(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    async with sessions() as session:
        found = await notes.load_encounter(session, encounter.session_id)

    assert found is not None
    assert found.id == encounter.id


async def test_an_unknown_session_has_no_encounter(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        assert await notes.load_encounter(session, uuid.uuid4()) is None


async def test_a_soft_deleted_encounter_is_not_found(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    """Nothing should still be writing notes for a retired encounter."""
    async with sessions() as session:
        await session.execute(
            sa.update(Encounter)
            .where(Encounter.id == encounter.id)
            .values(deleted_at=sa.func.now())
        )
        await session.commit()

    async with sessions() as session:
        assert await notes.load_encounter(session, encounter.session_id) is None

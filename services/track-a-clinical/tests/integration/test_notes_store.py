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

from hipaa_logger import AuditAction, close_pool, configure
from track_a_clinical import notes
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
            await session.scalar(statement, {"sid": session_id, "action": AuditAction.WRITE_NOTE})
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


async def audit_rows_for_action(
    sessions: async_sessionmaker[AsyncSession], session_id: uuid.UUID, action: AuditAction
) -> int:
    """Count audit rows for one session and one action.

    A parametrised sibling of :func:`count_audit_rows`, which hardcodes
    ``WRITE_NOTE``. TASK-053 writes a different action against the same session,
    and a helper that could only count one of them would make "the write-back
    audited" and "the note generation audited" indistinguishable.
    """
    statement = sa.text(
        "SELECT count(*) FROM audit_log WHERE session_id = :sid AND action = :action"
    )
    async with sessions() as session:
        return int(await session.scalar(statement, {"sid": session_id, "action": action}) or 0)


async def store_first_note(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> ClinicalNote:
    """Write a note for `encounter` and return the stored row."""
    async with sessions() as session:
        await notes.store_note(session, encounter=encounter, note=FIRST)
    stored = await notes_for(sessions, encounter.id)
    return stored[0]


async def test_the_document_reference_is_recorded_on_the_note(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    """The happy path: TASK-053's write-back records what the EHR created."""
    note = await store_first_note(sessions, encounter)

    async with sessions() as session:
        recorded = await notes.record_ehr_document_ref(
            session,
            encounter=encounter,
            note=await session.get_one(ClinicalNote, note.id),
            ehr_document_ref_id="DocumentReference-11",
        )

    assert recorded is not None
    # Read back from a fresh session rather than trusting the returned instance:
    # the update was a Core statement, so an object that reported the new value
    # without the explicit refresh would be reporting what the caller passed in.
    assert (await notes_for(sessions, encounter.id))[0].ehr_document_ref_id == (
        "DocumentReference-11"
    )


async def test_a_second_document_reference_is_refused(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    """Write-once against a real database, which is where the guard actually runs.

    The unit tests assert the 409 the route answers; this asserts the property
    that makes it safe — the conditional update itself declines, so a caller that
    skipped the pre-check would still not get a second document recorded.
    """
    note = await store_first_note(sessions, encounter)

    # Two sessions, and the loser loads its row *before* the winner writes. That
    # is what a real race looks like: an instance whose column still reads NULL,
    # held by a caller whose pre-check has already passed.
    #
    # **Do not simulate this by setting the loaded object's column to None.** It
    # marks the instance dirty, SQLAlchemy autoflushes that NULL before running
    # the statement, and the conditional update then matches — so the test passes
    # while asserting nothing about the guard. Verified against this database:
    # the update returned an id, and the row's real value was restored only by
    # the rollback. That version of this test was green in the way a guard test
    # must never be green.
    async with sessions() as loser, sessions() as winner:
        stale = await loser.get_one(ClinicalNote, note.id)

        await notes.record_ehr_document_ref(
            winner,
            encounter=encounter,
            note=await winner.get_one(ClinicalNote, note.id),
            ehr_document_ref_id="DocumentReference-first",
        )

        second = await notes.record_ehr_document_ref(
            loser,
            encounter=encounter,
            note=stale,
            ehr_document_ref_id="DocumentReference-second",
        )

    assert second is None
    assert (await notes_for(sessions, encounter.id))[0].ehr_document_ref_id == (
        "DocumentReference-first"
    )


async def test_recording_audits_once_and_a_refusal_audits_not_at_all(
    sessions: async_sessionmaker[AsyncSession], encounter: Encounter
) -> None:
    """A refused write recorded nothing, so a row saying otherwise would be a lie."""
    note = await store_first_note(sessions, encounter)

    # The same two-session race as above, for the same reason: a stale instance
    # has to come from a session that loaded it first, never from mutating one.
    async with sessions() as loser, sessions() as winner:
        stale = await loser.get_one(ClinicalNote, note.id)

        await notes.record_ehr_document_ref(
            winner,
            encounter=encounter,
            note=await winner.get_one(ClinicalNote, note.id),
            ehr_document_ref_id="DocumentReference-first",
        )
        await notes.record_ehr_document_ref(
            loser,
            encounter=encounter,
            note=stale,
            ehr_document_ref_id="DocumentReference-second",
        )

    assert (
        await audit_rows_for_action(sessions, encounter.session_id, AuditAction.WRITE_NOTE_TO_EHR)
    ) == 1

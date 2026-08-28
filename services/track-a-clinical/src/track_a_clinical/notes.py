"""Storing a generated note, once per encounter.

The write is idempotent because its caller is a Redis consumer and pub/sub
delivery is not exactly-once. A redelivered ``session:ended`` signal, a consumer
reconnect, or a retry of a generation that failed after its LLM calls would each
arrive here with a note for an encounter that already has one — and none of them
is an error worth raising, because the note that exists is the note that was
wanted.

``ON CONFLICT DO NOTHING`` on ``uq_clinical_notes_encounter`` rather than a
read-then-branch: two deliveries racing would both see no row and both insert,
and the loser would surface as an integrity error on what is an ordinary retry.
The same shape as ``_record_policy`` in track-b-rag, with ``DO NOTHING`` rather
than ``DO UPDATE`` because the first note generated for an encounter is the one
to keep — a retry knows nothing the attempt before it did not, and TASK-032's
provider edits must not be quietly replaced by a late duplicate signal.

**Every column written here is PHI**, so the insert is audited. The audit row
joins this transaction: a note that exists with no audit row, and an audit row
for a note that rolled back, are both worse than the write failing outright.
"""

from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from track_a_clinical import audit
from track_a_clinical.models import ClinicalNote, Encounter, dump_codes
from track_a_clinical.soap import GeneratedNote

logger = logging.getLogger(__name__)


async def load_encounter(session: AsyncSession, session_id: uuid.UUID) -> Encounter | None:
    """Return the encounter for a session id, or None if there is none.

    Soft-deleted rows are excluded, matching every other reader of this table:
    a deleted encounter is one nothing should still be writing notes for.
    """
    result: Encounter | None = await session.scalar(
        sa.select(Encounter).where(
            Encounter.session_id == session_id,
            Encounter.deleted_at.is_(None),
        )
    )
    return result


async def store_note(
    session: AsyncSession,
    *,
    encounter: Encounter,
    note: GeneratedNote,
) -> uuid.UUID | None:
    """Insert the note for `encounter` and return its id, or None if one existed.

    Returning None is the ordinary outcome of a duplicate delivery, not a
    failure. Nothing is audited in that case: this call wrote no note, and an
    audit row claiming a PHI write that did not happen is worse than no row.

    Args:
        session: The session whose transaction the insert and its audit join.
        encounter: The encounter the note belongs to, already loaded — its
            ``provider_id`` is the audit's actor.
        note: What :func:`track_a_clinical.soap.generate` produced.

    Returns:
        The new row's id, or None when the encounter already had a note.
    """
    statement = (
        pg_insert(ClinicalNote)
        .values(
            encounter_id=encounter.id,
            soap_subjective=note.sections.subjective,
            soap_objective=note.sections.objective,
            soap_assessment=note.sections.assessment,
            soap_plan=note.sections.plan,
            # None rather than [] when extraction failed: the column records
            # that the codes were never determined, not that there were none.
            icd10_codes=dump_codes(note.icd10_codes) if note.icd10_codes is not None else None,
            cpt_codes=dump_codes(note.cpt_codes) if note.cpt_codes is not None else None,
        )
        .on_conflict_do_nothing(constraint="uq_clinical_notes_encounter")
        .returning(ClinicalNote.id)
    )
    note_id = await session.scalar(statement)

    if note_id is None:
        # The signal arrived twice, or a retry followed a write that had in fact
        # landed. Roll back rather than commit: there is nothing to commit, and
        # leaving the transaction open would hold the row locks taken above.
        await session.rollback()
        logger.info(
            "Encounter %s already has a note; this generation is a duplicate and was discarded",
            encounter.id,
        )
        return None

    await audit.audit_note_write(
        session,
        note_id=note_id,
        session_id=encounter.session_id,
        provider_id=encounter.provider_id,
    )
    await session.commit()
    logger.info("Stored clinical note %s for encounter %s", note_id, encounter.id)
    return note_id

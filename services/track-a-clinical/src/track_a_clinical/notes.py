"""Storing a generated note once per encounter, and serving it back for review.

The write half is TASK-030's, driven by a Redis signal with no request behind
it. The read and edit half is TASK-032's, driven by a provider's request. They
share this module because they are the same row and the same invariant; they
differ in who the actor is and what the audit trail records.

TASK-053 adds a third writer with a third caller again — ``fhir-integration``,
recording the ``DocumentReference`` id it created when filing the note to an
EHR. It writes one column no other path may write, and it is the one write here
whose guard has to survive a race, because losing that race would put a second
copy of a note on a patient's chart.


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
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hipaa_logger import AuditAction
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


#: The fields a provider edits that are *content*. A change to any of them is
#: what ``provider_edited`` records. ``reviewed_by_provider`` is deliberately not
#: here: marking a note reviewed is an attestation about the note, not an edit of
#: it, and conflating the two would make every review look like a rewrite.
CONTENT_FIELDS: Final = (
    "soap_subjective",
    "soap_objective",
    "soap_assessment",
    "soap_plan",
    "icd10_codes",
    "cpt_codes",
)

#: The two content fields holding :class:`ExtractedCode` entries rather than text.
CODE_FIELDS: Final = frozenset({"icd10_codes", "cpt_codes"})


async def load_note(session: AsyncSession, *, encounter: Encounter) -> ClinicalNote | None:
    """Return the encounter's note, or None when TASK-030 has not written one.

    Zero or one row, never two: ``uq_clinical_notes_encounter`` enforces it and
    :func:`store_note` writes through it, so this does not order or tie-break.

    None here is not the same answer as an unknown encounter, and the routes keep
    them apart — a review screen has to be able to say "the note is not ready
    yet" rather than "this visit does not exist".
    """
    result: ClinicalNote | None = await session.scalar(
        sa.select(ClinicalNote).where(
            ClinicalNote.encounter_id == encounter.id,
            ClinicalNote.deleted_at.is_(None),
        )
    )
    return result


async def apply_note_edits(
    session: AsyncSession,
    *,
    encounter: Encounter,
    note: ClinicalNote,
    edits: dict[str, Any],
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> ClinicalNote:
    """Apply a provider's partial edit to a stored note and audit it.

    Args:
        session: The session whose transaction the update and its audit join.
        encounter: The note's encounter — its ``provider_id`` is the audit actor.
        note: The row to edit, already loaded.
        edits: Only the fields the request actually carried. The caller builds
            this from ``model_fields_set``, so a field the client omitted is
            absent here and a field the client explicitly set to ``null`` is
            present with a ``None`` value. Those are different requests and this
            function must not collapse them: reading an omitted ``icd10_codes``
            as ``[]`` would let a provider fixing a typo in the plan section
            silently declare the encounter has no diagnoses. See CLAUDE.md, "So
            an editing endpoint needs three states, not two".
        ip_address: Client IP, for the audit row.
        user_agent: Client user agent, for the audit row.

    Returns:
        The same note instance, updated.
    """
    content_changed = False
    for field, value in edits.items():
        new_value = dump_codes(value) if field in CODE_FIELDS and value is not None else value
        if new_value != getattr(note, field):
            setattr(note, field, new_value)
            content_changed = content_changed or field in CONTENT_FIELDS

    # Set on a real change rather than on the mere arrival of a PATCH: a client
    # re-sending the text it was given has edited nothing, and a flag that says
    # otherwise stops meaning anything. Never cleared — a note that was edited
    # stays edited even if the provider restores the original wording.
    if content_changed:
        note.provider_edited = True

    # Audited whether or not anything changed. The row was read and written
    # against on a request, which is the access the trail is asked about; a
    # no-op edit is still someone opening a patient's note.
    await audit.audit_note_access(
        session,
        action=AuditAction.UPDATE_NOTE,
        note_id=note.id,
        session_id=encounter.session_id,
        provider_id=encounter.provider_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    logger.info(
        "Updated clinical note %s for encounter %s (content_changed=%s)",
        note.id,
        encounter.id,
        content_changed,
    )
    return note


async def record_ehr_document_ref(
    session: AsyncSession,
    *,
    encounter: Encounter,
    note: ClinicalNote,
    ehr_document_ref_id: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> ClinicalNote | None:
    """Record the ``DocumentReference`` a write-back created, once and only once.

    **The guard is the ``WHERE`` clause, not a prior read.** A caller that checks
    ``ehr_document_ref_id is None`` and then updates has a window between the two
    in which a second caller does the same, and both file a document — which is a
    duplicate entry on a patient's chart, the specific harm TASK-053 refuses. So
    the update is conditional on the column still being NULL and its ``RETURNING``
    decides: no id came back means somebody else got there first, and the caller
    turns that into a 409. The same shape as :func:`store_note`'s conflict
    handling one function up, for the same reason.

    Returning None is therefore not a failure of this function; it is this
    function working. The caller distinguishes it from success.

    Args:
        session: The session whose transaction the update and its audit join.
        encounter: The note's encounter — its ``provider_id`` is the audit actor,
            per the rule that an actor comes from the row rather than the caller.
        note: The row to record against, already loaded.
        ehr_document_ref_id: The id the EHR assigned the created document.
        ip_address: Client IP, for the audit row.
        user_agent: Client user agent, for the audit row.

    Returns:
        The updated note, or None when the note had already been filed.
    """
    # Read before the statement runs. ``session.rollback()`` below expires every
    # instance in the session — unconditionally, unlike commit, which
    # ``expire_on_commit=False`` opts out of — so touching ``note.id`` afterwards
    # would lazy-load it, and lazy IO in an async session raises MissingGreenlet.
    # That would turn the ordinary refusal this branch exists to report into a
    # 500 from the route.
    note_id = note.id

    updated_id = await session.scalar(
        sa.update(ClinicalNote)
        .where(
            ClinicalNote.id == note.id,
            ClinicalNote.ehr_document_ref_id.is_(None),
        )
        .values(ehr_document_ref_id=ehr_document_ref_id)
        .returning(ClinicalNote.id)
    )

    if updated_id is None:
        # Roll back rather than commit: nothing was written, and an audit row
        # claiming a note was filed to an EHR when this call filed nothing would
        # be the same lie in the trail that a duplicate chart entry is on the
        # chart. The row locks taken above are released with it.
        await session.rollback()
        logger.info(
            "Note %s already carries an EHR document reference; this record was refused",
            note_id,
        )
        return None

    await audit.audit_note_access(
        session,
        action=AuditAction.WRITE_NOTE_TO_EHR,
        note_id=note_id,
        session_id=encounter.session_id,
        provider_id=encounter.provider_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    # The update above was a Core statement, so the loaded object still carries
    # the old NULL — and ``expire_on_commit=False`` means a commit will not
    # reload it either (see ``db.get_sessionmaker``). Refresh explicitly, or the
    # response would report the column as empty on the one call that filled it.
    await session.refresh(note)
    logger.info(
        "Recorded EHR document reference for note %s on encounter %s",
        note_id,
        encounter.id,
    )
    return note

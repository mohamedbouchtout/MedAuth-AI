"""Assembling one encounter's prior-authorization bundle. TASK-060.

The read half loads what the encounter produced — its note, its nudges and the
patient's chart context — and the write half records one ``prior_auth_requests``
row from it. Nothing here decides where the request is sent; that is TASK-061.

Four rules shape it, and each closes a way the assembly could go quietly wrong:

**One bundle per encounter.** The insert is ``ON CONFLICT DO NOTHING`` against
``uq_prior_auth_requests_encounter``, because the caller is a Redis consumer and
pub/sub delivery is not exactly-once. This is TASK-030's fix on the identical
problem shape rather than a second idempotency design, and the consequence here
is worse: TASK-061 submits from these rows, so a duplicate is a payer asked to
open two reviews of one request. ``DO NOTHING`` rather than ``DO UPDATE``
because a retry knows nothing the attempt before it did not, and because a
duplicate signal arriving after TASK-061 has run would otherwise overwrite
``submission_method``, ``payer_reference_number``, ``submitted_at`` and
``payer_outcome`` — discarding what a payer actually said.

**Only a coded procedure can be requested.** A nudge with no CPT code (TASK-044
raises those on a keyword that resolved to none) is not something a payer can be
asked to authorize: ``PriorAuthProcedure.cpt_code`` is required at the
submission boundary, so such an entry would fail there rather than here. It is
excluded and counted in the log instead.

**Nothing a machine merely suggested is claimed as a diagnosis.** ``diagnoses``
is filtered to ``llm-extraction`` and ``provider-accepted`` entries, per
CLAUDE.md's shape contract. A ``comprehend-medical`` entry is a code no provider
stated, and a bundle asserts to a payer what the provider documented. The
submitter's builder filters again independently — that is belt and braces rather
than duplication, and it matters because this row is also read by the dashboard
and by anything else that comes to treat it as the record of what was requested.

**The audit row joins the transaction.** A bundle that exists with no audit row,
and an audit row for a bundle that rolled back, are both worse than the write
failing outright.

Everything read here is PHI. No log line in this module carries a procedure, a
diagnosis, a criterion or a note excerpt — only identifiers and counts.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from prior_auth import audit, evidence
from track_a_clinical.models import (
    PRIOR_AUTH_STATUS_PENDING,
    SOURCE_LLM_EXTRACTION,
    SOURCE_PROVIDER_ACCEPTED,
    ClinicalNote,
    ClinicalNudge,
    Encounter,
    PriorAuthRequest,
    load_codes,
)

logger = logging.getLogger(__name__)

#: The ``source`` values a diagnosis may carry out of this system. Both mean a
#: provider stated or accepted the code; ``comprehend-medical`` means a machine
#: surfaced it and nobody has. See CLAUDE.md, "Writing clinical data out to the
#: EHR" and the extracted-code shape contract.
CLAIMABLE_SOURCES: Final = frozenset({SOURCE_LLM_EXTRACTION, SOURCE_PROVIDER_ACCEPTED})

#: The constraint the insert conflicts on. Named rather than inferred so a
#: rename in track-a-clinical's migration breaks the insert loudly here.
UNIQUE_CONSTRAINT: Final = "uq_prior_auth_requests_encounter"


@dataclass(frozen=True)
class AssembledBundle:
    """What one encounter's nudges, note and coverage add up to.

    Held as a value before it is written so the assembly is testable without a
    database, and so the write is a single statement over finished data.
    """

    procedures: list[dict[str, Any]]
    diagnoses: list[dict[str, Any]]
    clinical_evidence: list[dict[str, Any]]
    payer_name: str | None
    #: Nudges dropped for carrying no CPT code. Not an error — see the module
    #: docstring — but the log says how many, because an encounter whose whole
    #: nudge set was keyword-only produces no bundle at all.
    uncoded_nudges: int


async def load_encounter(session: AsyncSession, session_id: uuid.UUID) -> Encounter | None:
    """Return the encounter for a session id, or None if there is none.

    Soft-deleted rows are excluded, matching every other reader of this table.
    """
    result: Encounter | None = await session.scalar(
        sa.select(Encounter).where(
            Encounter.session_id == session_id,
            Encounter.deleted_at.is_(None),
        )
    )
    return result


async def load_note(session: AsyncSession, *, encounter: Encounter) -> ClinicalNote | None:
    """Return the encounter's generated note, or None when TASK-030 has not written one.

    Zero or one row, never two: ``uq_clinical_notes_encounter`` enforces it and
    TASK-030's insert writes through it, so this neither orders nor tie-breaks.
    None is the ordinary answer while generation is still running, which is what
    the caller's backoff is for.
    """
    result: ClinicalNote | None = await session.scalar(
        sa.select(ClinicalNote).where(
            ClinicalNote.encounter_id == encounter.id,
            ClinicalNote.deleted_at.is_(None),
        )
    )
    return result


async def load_nudges(session: AsyncSession, *, encounter: Encounter) -> list[ClinicalNudge]:
    """Return the nudges fired during the encounter, oldest first.

    Ordered by ``fired_at`` so the assembled bundle is deterministic — two runs
    over one encounter must produce the same procedures in the same order, or a
    reviewer comparing a resubmission against the original sees differences that
    are not differences. ``clinical_nudges`` has no ``deleted_at`` column, by the
    same TASK-005 decision that leaves ``prior_auth_requests`` without one.
    """
    result = await session.scalars(
        sa.select(ClinicalNudge)
        .where(ClinicalNudge.encounter_id == encounter.id)
        .order_by(ClinicalNudge.fired_at, ClinicalNudge.id)
    )
    return list(result)


def claimable_diagnoses(note: ClinicalNote | None) -> list[dict[str, Any]]:
    """Return the note's ICD-10 codes that may be asserted to a payer.

    ``comprehend-medical`` entries are dropped: they are codes the validating
    pass surfaced that no provider ever stated. The way one becomes claimable is
    unchanged and is the whole point of the ``provider-accepted`` value — a
    provider accepts it through ``PATCH /notes/{session_id}``, which rewrites its
    ``source``.

    A ``null`` column and an empty one are both answered with an empty list
    here, and the distinction is deliberately not carried forward: it matters to
    a review screen deciding whether extraction ran, and means nothing to a
    payer, which is told what is being claimed rather than what was attempted.
    """
    if note is None:
        return []
    codes = load_codes(note.icd10_codes)
    return [code.model_dump(mode="json") for code in codes if code.source in CLAIMABLE_SOURCES]


def build_bundle(
    *,
    note: ClinicalNote | None,
    nudges: list[ClinicalNudge],
    encounter_payer: str | None,
    coverage_payer: str | None,
) -> AssembledBundle:
    """Turn one encounter's note and nudges into the row's three JSONB columns.

    Args:
        note: The generated SOAP note, or None.
        nudges: Every nudge fired during the encounter, oldest first.
        encounter_payer: ``encounters.insurance_payer`` — the payer's own
            display name, copied from the ``Coverage`` at session start.
        coverage_payer: The payer name the chart context just returned, used
            only when the encounter carries none. Both are display names for the
            same fact, so this joins two spellings rather than substituting a
            different payer. **Never a ``payer_vocab`` slug**: a slug is for
            matching our own indexed policies, and this value goes to the payer.

    Returns:
        The assembled value. ``procedures`` may be empty, which the caller reads
        as "there is nothing to request authorization for".
    """
    procedures: list[dict[str, Any]] = []
    coded: list[ClinicalNudge] = []
    seen: set[str] = set()
    uncoded = 0

    for nudge in nudges:
        code = (nudge.cpt_code or "").strip()
        if not code:
            uncoded += 1
            continue
        if code in seen:
            # The partial unique index already stops a second nudge per code per
            # encounter, so this is belt to that braces rather than a live case.
            continue
        seen.add(code)
        coded.append(nudge)
        procedures.append({"cpt_code": code, "description": nudge.procedure_name})

    return AssembledBundle(
        procedures=procedures,
        diagnoses=claimable_diagnoses(note),
        # Evidence is built from the coded nudges only, so the bundle never
        # offers documentation for a procedure it is not requesting.
        clinical_evidence=evidence.build_evidence(note, coded),
        payer_name=encounter_payer or coverage_payer,
        uncoded_nudges=uncoded,
    )


async def store_bundle(
    session: AsyncSession,
    *,
    encounter: Encounter,
    bundle: AssembledBundle,
) -> uuid.UUID | None:
    """Insert the bundle for `encounter` and return its id, or None if one existed.

    Returning None is the ordinary outcome of a duplicate delivery, not a
    failure. Nothing is audited in that case: this call assembled no bundle, and
    an audit row claiming a PHI write that did not happen is worse than no row.

    Args:
        session: The session whose transaction the insert and its audit join.
        encounter: The encounter the bundle belongs to, already loaded — its
            ``provider_id`` is the audit's actor.
        bundle: What :func:`build_bundle` produced.

    Returns:
        The new row's id, or None when the encounter already had a bundle.
    """
    # Read before the statement runs, for the reason track-a-clinical's
    # ``store_note`` documents: the rollback below expires every instance in
    # this session, and the caller passes an encounter loaded through this same
    # session, so reading these afterwards would lazy-load and raise
    # MissingGreenlet — turning an ordinary redelivery into a reported failure.
    encounter_id = encounter.id
    session_id = encounter.session_id
    provider_id = encounter.provider_id

    statement = (
        pg_insert(PriorAuthRequest)
        .values(
            encounter_id=encounter_id,
            status=PRIOR_AUTH_STATUS_PENDING,
            payer_name=bundle.payer_name,
            procedures=bundle.procedures,
            diagnoses=bundle.diagnoses,
            clinical_evidence=bundle.clinical_evidence,
        )
        .on_conflict_do_nothing(constraint=UNIQUE_CONSTRAINT)
        .returning(PriorAuthRequest.id)
    )
    request_id = await session.scalar(statement)

    if request_id is None:
        # The signal arrived twice, or a retry followed a write that had in fact
        # landed. Roll back rather than commit: there is nothing to commit, and
        # leaving the transaction open would hold the row locks taken above.
        await session.rollback()
        logger.info(
            "Encounter %s already has a prior-authorization request; this assembly "
            "is a duplicate and was discarded",
            encounter_id,
        )
        return None

    await audit.audit_bundle_write(
        session,
        request_id=request_id,
        session_id=session_id,
        provider_id=provider_id,
    )
    await session.commit()
    logger.info(
        "Assembled prior-authorization request %s for encounter %s "
        "(%d procedure(s), %d diagnosis(es), %d evidence entr(ies))",
        request_id,
        encounter_id,
        len(bundle.procedures),
        len(bundle.diagnoses),
        len(bundle.clinical_evidence),
    )
    return request_id

"""Note review endpoints — what a provider reads and edits after a visit (TASK-032).

``GET /notes/{session_id}`` returns the SOAP note TASK-030's consumer generated
from the encounter's transcript. ``PATCH /notes/{session_id}`` applies a
provider's edits to it. Both are the server side of TASK-071's review screen, and
``PATCH`` is also how a machine-suggested diagnosis becomes documentation a
prior-auth bundle may claim.

**Keyed on ``session_id``, not on ``encounters.id``.** Earlier drafts of TASK-032
said ``/notes/{encounter_id}``, naming an identifier no client has ever been
given: ``POST /sessions/start`` returns ``{session_id, jwt}`` and nothing here
exposes the encounter's primary key. Every route and every Redis channel in this
service is keyed on the session, and one route with a second name for the same
visit would only guarantee that clients eventually send the wrong one.

**No credential in v1.** These routes carry no session token, and the actor comes
from the ``encounters`` row rather than from anything the caller sent — the same
strength as ``POST /sessions/start``, which takes ``provider_id`` as an
unauthenticated body field. In particular ``validate_remint_credential`` is *not*
reused: it answers 409 for a completed encounter, and note review happens only on
completed encounters, so requiring it would make every note unreadable. The
reasoning and the Phase 5 successor are in CLAUDE.md's session section; do not
re-derive either here.

**Everything these routes return is PHI** — a clinical note in full. Both audit,
per Known Constraints #6, and no log line in this module carries note text, a
patient identifier, or a rejected field value.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from api_envelope import ApiHTTPException, ApiResponse, error_responses
from hipaa_logger import AuditAction
from track_a_clinical import audit, notes
from track_a_clinical.api.dependencies import get_db_session
from track_a_clinical.api.schemas import (
    NoteData,
    NoteEhrReferenceData,
    RecordEhrReferenceRequest,
    UpdateNoteRequest,
)
from track_a_clinical.models import ClinicalNote, Encounter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notes", tags=["notes"])

ERROR_CODE_SESSION_NOT_FOUND = "session_not_found"
#: Distinct from ``session_not_found`` on purpose, and the distinction is the
#: point rather than a nicety: "the visit does not exist" and "the note has not
#: been generated yet" send a provider to two different places. The second is
#: also the ordinary state for a few seconds after a visit ends, while TASK-030's
#: Sonnet call is still running.
ERROR_CODE_NOTE_NOT_GENERATED = "note_not_generated"
#: Refusing a second write-back rather than recording it (TASK-053). A note
#: already filed to a chart is not re-filed: two ``DocumentReference``
#: resources for one encounter is duplicate clinical documentation, which is a
#: clinician reading one version while another is amended.
ERROR_CODE_NOTE_ALREADY_WRITTEN = "note_already_written_to_ehr"

#: Both failures are 404, so the generic per-status wording would describe only
#: one of them. Spelled out here for the published spec.
NOTE_ERROR_DESCRIPTIONS = {
    status.HTTP_404_NOT_FOUND: (
        "Either the session is unknown or soft-deleted (`session_not_found`), or "
        "it exists but no note has been generated for it yet "
        "(`note_not_generated`) — the two are different situations and carry "
        "different error codes."
    ),
}


def _client_ip(request: Request) -> str | None:
    """Return the requesting client's IP, or None when the transport has no peer."""
    return request.client.host if request.client else None


async def _load_encounter_and_note(
    session: AsyncSession,
    session_id: uuid.UUID,
) -> tuple[Encounter, ClinicalNote]:
    """Resolve a session to its encounter and that encounter's note.

    Raises:
        ApiHTTPException: 404 ``session_not_found`` when no live encounter has
            this session id, and 404 ``note_not_generated`` when one does but
            TASK-030 has not written its note.
    """
    encounter = await session.scalar(
        sa.select(Encounter).where(
            Encounter.session_id == session_id,
            Encounter.deleted_at.is_(None),
        )
    )
    if encounter is None:
        raise ApiHTTPException(
            status.HTTP_404_NOT_FOUND,
            ERROR_CODE_SESSION_NOT_FOUND,
            f"No encounter for session {session_id}",
        )

    note = await notes.load_note(session, encounter=encounter)
    if note is None:
        raise ApiHTTPException(
            status.HTTP_404_NOT_FOUND,
            ERROR_CODE_NOTE_NOT_GENERATED,
            f"Session {session_id} has no generated note yet",
        )
    return encounter, note


@router.get(
    "/{session_id}",
    response_model=ApiResponse[NoteData],
    summary="Read a session's SOAP note",
    response_description="The generated note, its extracted codes and its review flags.",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=NOTE_ERROR_DESCRIPTIONS,
    ),
)
async def read_note(
    session_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[NoteData]:
    """Return the SOAP note generated for a session.

    The response carries the four SOAP sections, both code lists in the shape
    fixed in CLAUDE.md's "Extracted clinical codes" contract, and the review
    flags. ``icd10_codes`` and ``cpt_codes`` are ``null`` when the extraction
    pass never answered and ``[]`` when it ran and found nothing — different
    facts, kept apart all the way to the client.

    **This read never sets ``reviewed_by_provider``.** That column records a
    provider's attestation, and a read that flips it would turn it into a record
    that a screen was loaded, which an auditor cannot tell apart afterwards. Mark
    a note reviewed by sending the field to ``PATCH``.

    Returns 404 with ``session_not_found`` for an unknown or soft-deleted
    session, and 404 with ``note_not_generated`` when the visit exists but its
    note has not been written yet — which is the ordinary state for a few seconds
    after a visit ends.
    """
    encounter, note = await _load_encounter_and_note(session, session_id)

    await audit.audit_note_access(
        session,
        action=AuditAction.READ_NOTE,
        note_id=note.id,
        session_id=session_id,
        provider_id=encounter.provider_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # Nothing above changed a row, so this commits the audit write alone — which
    # is the point: reading a patient's note is the access being recorded.
    await session.commit()

    return ApiResponse[NoteData](data=NoteData.from_row(session_id=session_id, note=note))


@router.patch(
    "/{session_id}",
    response_model=ApiResponse[NoteData],
    summary="Edit a session's SOAP note",
    response_description="The note as it stands after the edit.",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=NOTE_ERROR_DESCRIPTIONS,
    ),
)
async def update_note(
    session_id: uuid.UUID,
    body: UpdateNoteRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[NoteData]:
    """Apply a provider's edits to a stored note.

    A partial update: send only the fields being changed. **An omitted field is
    left alone and a field explicitly set to ``null`` is cleared** — the handler
    works from ``model_fields_set``, so those two requests cannot be confused. A
    client that sends ``icd10_codes: []`` because it did not think to omit it
    would declare that the encounter has no diagnoses, which is why the
    distinction is enforced here rather than trusted to callers.

    ``provider_edited`` is set by the server when a content field actually
    changes, and is not something a client sends. ``reviewed_by_provider`` is the
    opposite: it is a provider's attestation, so it is set only by being sent
    explicitly, and marking a note reviewed without editing it leaves
    ``provider_edited`` false.

    A ``comprehend-medical`` entry re-sent with ``source: "provider-accepted"``
    becomes documentation — that is how a machine suggestion turns into a
    diagnosis TASK-060 may claim in a prior-auth bundle. Such an entry carries no
    ``confidence`` and no ``validation``; the model rejects both, because a human
    acceptance is a fact rather than a probability. See CLAUDE.md's shape
    contract.

    Returns 404 with ``session_not_found`` or ``note_not_generated``, exactly as
    the read does, and 422 for a body that sets no fields at all.
    """
    encounter, note = await _load_encounter_and_note(session, session_id)

    updated = await notes.apply_note_edits(
        session,
        encounter=encounter,
        note=note,
        edits=body.edited_fields(),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return ApiResponse[NoteData](data=NoteData.from_row(session_id=session_id, note=updated))


@router.get(
    "/{session_id}/ehr-reference",
    response_model=ApiResponse[NoteEhrReferenceData],
    summary="Read a note's EHR linkage",
    response_description="The identifiers a write-back needs, and whether one has happened.",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=NOTE_ERROR_DESCRIPTIONS,
    ),
)
async def read_note_ehr_reference(
    session_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[NoteEhrReferenceData]:
    """Return the join between this note and the EHR's record of the same visit.

    **Server-to-server, for the note write-back (TASK-053).** It answers the two
    questions ``fhir-integration`` cannot answer for itself — which chart entry
    this visit is (``ehr_encounter_id``), and who the document is about
    (``patient_fhir_id``) — plus whether the note has already been filed, which
    is what lets a repeat write be refused before an EHR is ever called.

    A null ``ehr_encounter_id`` is an ordinary answer, not an error here: the
    visit was started outside a SMART launch. The caller decides what to do about
    it, and TASK-053's answer is to refuse rather than address a guessed chart.

    This returns a patient identifier, so it is a PHI read and audits as
    ``READ_ENCOUNTER`` against the encounter — the row that holds both
    identifiers it exists to serve.
    """
    encounter, note = await _load_encounter_and_note(session, session_id)

    await audit.audit_encounter_access(
        session,
        action=AuditAction.READ_ENCOUNTER,
        encounter_id=encounter.id,
        session_id=session_id,
        provider_id=encounter.provider_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()

    return ApiResponse[NoteEhrReferenceData](
        data=NoteEhrReferenceData.from_rows(encounter=encounter, note=note)
    )


@router.patch(
    "/{session_id}/ehr-reference",
    response_model=ApiResponse[NoteEhrReferenceData],
    summary="Record the EHR document a write-back created",
    response_description="The note's EHR linkage, now carrying the document id.",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=NOTE_ERROR_DESCRIPTIONS
        | {
            status.HTTP_409_CONFLICT: (
                "This note has already been filed to the EHR "
                "(`note_already_written_to_ehr`). Refused rather than recorded "
                "twice: two documents for one encounter is a duplicate entry on "
                "a patient's chart."
            ),
        },
    ),
)
async def record_note_ehr_reference(
    session_id: uuid.UUID,
    body: RecordEhrReferenceRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[NoteEhrReferenceData]:
    """Record the ``DocumentReference`` id an EHR assigned to this note.

    **This is the only writer of ``clinical_notes.ehr_document_ref_id``.**
    ``PATCH /notes/{session_id}`` still forbids the field and that has not been
    relaxed for this: a provider's browser must not be able to claim a note was
    filed. This route is called by ``fhir-integration`` after it has actually
    created the document, which is why the id it carries can be trusted to name
    something real.

    **Write-once, enforced by the update itself.** A note that already carries a
    document id answers 409 — and the check is the update's own ``WHERE`` clause
    rather than a preceding read, so two concurrent write-backs cannot both
    succeed. The caller that loses learns it lost.

    The actor is the encounter's provider, never the calling service: a
    service-to-service hop does not change whose visit this is.
    """
    encounter, note = await _load_encounter_and_note(session, session_id)

    recorded = await notes.record_ehr_document_ref(
        session,
        encounter=encounter,
        note=note,
        ehr_document_ref_id=body.ehr_document_ref_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if recorded is None:
        raise ApiHTTPException(
            status.HTTP_409_CONFLICT,
            ERROR_CODE_NOTE_ALREADY_WRITTEN,
            f"Session {session_id} already has a note filed to the EHR",
        )

    return ApiResponse[NoteEhrReferenceData](
        data=NoteEhrReferenceData.from_rows(encounter=encounter, note=recorded)
    )

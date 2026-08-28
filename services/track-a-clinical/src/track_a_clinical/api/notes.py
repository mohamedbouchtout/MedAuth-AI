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
from track_a_clinical import audit, notes
from track_a_clinical.api.dependencies import get_db_session
from track_a_clinical.api.schemas import NoteData, UpdateNoteRequest
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
        action=audit.ACTION_READ_NOTE,
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

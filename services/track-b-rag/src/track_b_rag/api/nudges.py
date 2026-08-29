"""``PATCH /nudges/{nudge_id}/acknowledge`` — a provider dismissing an alert.

TASK-040 stores a ``clinical_nudges`` row before publishing it, precisely so
this route has something real to update: the ``nudge_id`` in the payload
TASK-041 relays is that row's primary key, and a client can therefore only
dismiss a nudge that was actually recorded.

**This route touches PHI and audits**, like ``/policies/query`` next door and
unlike ``/policies/ingest``: a nudge names a procedure and the payer criteria an
identified encounter has not documented, which is the same judgement TASK-041
made in deciding that opening the relay is a PHI access.

**It carries no credential in v1** — CLAUDE.md, "A route keyed on a resource
rather than a session follows the same v1 rule". That is not an oversight to
patch locally: ``packages/session-auth`` validates a token's ``session_id``
claim against a ``session_id`` in the path, and this path has none, since
``nudge_id`` is the only identifier a client is given. Putting a session
segment in the path purely to make that validator fit would hand clients two
names for one nudge. The eventual fix is validating against a *resolved*
session id, and it is recorded in that section rather than improvised here.

**It is also the first route in this service a browser calls**, which is a
separate problem: nothing in this repository answers CORS, so TASK-042's dismiss
cannot reach this handler until TASK-041c settles that. Do not install
middleware for it here — one service quietly growing a permissive policy is how
a repo-wide decision gets made by accident.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from api_envelope import ApiHTTPException, ApiResponse, error_responses
from track_a_clinical.models import ClinicalNudge, Encounter
from track_b_rag import audit, db
from track_b_rag.api.dependencies import get_db_session
from track_b_rag.api.schemas import AcknowledgeNudgeData, AcknowledgeNudgeRequest

router = APIRouter(prefix="/nudges", tags=["nudges"])

ERROR_CODE_NUDGE_NOT_FOUND = "nudge_not_found"

#: What these statuses mean on this route specifically. The generic 404 wording
#: would not tell a client that a *live* nudge id can stop resolving because the
#: encounter behind it was retired.
NUDGE_ERROR_DESCRIPTIONS = {
    status.HTTP_404_NOT_FOUND: (
        "No such nudge, or the encounter it belongs to has been soft-deleted."
    ),
    status.HTTP_422_UNPROCESSABLE_CONTENT: (
        'The nudge_id is not a UUID, or the body is not {"acknowledged": true} — '
        "false is rejected rather than read as an un-acknowledge."
    ),
}


def _client_ip(request: Request) -> str | None:
    """Return the requesting client's IP, or None when the transport has no peer."""
    return request.client.host if request.client else None


@router.patch(
    "/{nudge_id}/acknowledge",
    response_model=ApiResponse[AcknowledgeNudgeData],
    summary="Acknowledge a nudge",
    response_description="The nudge's acknowledgement state and when it was recorded.",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=NUDGE_ERROR_DESCRIPTIONS,
    ),
)
async def acknowledge_nudge(
    nudge_id: uuid.UUID,
    body: AcknowledgeNudgeRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[AcknowledgeNudgeData]:
    """Mark a nudge as dismissed by the provider it was shown to.

    Sets `acknowledged` and `acknowledged_at`. This is what TASK-042's and
    TASK-043's dismiss buttons call, using the `nudge_id` carried in the nudge
    payload they were rendered from.

    Idempotent: a repeat call returns 200 with `already_acknowledged` true and
    the *original* `acknowledged_at`, so a double tap or a retried request
    cannot move a timestamp that records when a provider actually saw the alert.

    Returns 404 when the nudge is unknown or the encounter it belongs to has
    been soft-deleted — a retired encounter's nudges are not dismissible.

    Writes an `audit_log` row naming the encounter's provider: `ACKNOWLEDGE_NUDGE`
    when this call changed the row, `READ_NUDGE` when it did not.

    Carries no credential in v1. See the module docstring.
    """
    # One statement, and the join is the point: clinical_nudges has no
    # deleted_at of its own — a nudge records what a provider was told, so the
    # row is never retired — which means a soft-deleted encounter is invisible
    # from the nudge row alone. Selecting the nudge and checking it separately
    # would pass every other test here while leaving a retired encounter's
    # nudges mutable. The same select also resolves the actor, which this route
    # has no other honest source for.
    row = (
        await session.execute(
            sa.select(ClinicalNudge, Encounter.session_id, Encounter.provider_id)
            .join(Encounter, ClinicalNudge.encounter_id == Encounter.id)
            .where(
                ClinicalNudge.id == nudge_id,
                Encounter.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise ApiHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ERROR_CODE_NUDGE_NOT_FOUND,
            message=f"No nudge {nudge_id} on an active encounter",
        )

    nudge, session_uuid, provider_id = row

    already_acknowledged = nudge.acknowledged
    if not already_acknowledged:
        nudge.acknowledged = True
        # NOW() rather than a Python timestamp, so every service in this
        # monorepo dates a row from one clock.
        nudge.acknowledged_at = sa.func.now()

    await audit.audit_nudge_acknowledge(
        nudge_id=nudge.id,
        session_id=session_uuid,
        provider_id=provider_id,
        changed=not already_acknowledged,
        conn=await db.raw_asyncpg_connection(session),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    if not already_acknowledged:
        # acknowledged_at holds a SQL construct until it is read back.
        await session.refresh(nudge)

    return ApiResponse[AcknowledgeNudgeData](
        data=AcknowledgeNudgeData(
            nudge_id=nudge.id,
            acknowledged=nudge.acknowledged,
            # fired_at only stands in for a row acknowledged outside this
            # endpoint; the transition above always writes acknowledged_at.
            acknowledged_at=nudge.acknowledged_at or nudge.fired_at,
            already_acknowledged=already_acknowledged,
        )
    )

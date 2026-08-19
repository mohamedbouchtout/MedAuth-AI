"""Session lifecycle endpoints — the issuer every real-time service depends on.

``POST /sessions/start`` creates the ``encounters`` row and mints the session JWT
that ``audio-ingestion`` (TASK-020) and ``nudge-service`` (TASK-041) validate.
``POST /sessions/{session_id}/end`` closes the encounter and publishes the
``session:ended:{session_id}`` signal that SOAP generation (TASK-030) and prior
auth bundle assembly (TASK-060) both subscribe to.

Both rows and both signals key off ``session_id``, not the encounter primary key:
that is the identifier already travelling through every Redis channel name in
CLAUDE.md's canonical list.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from api_envelope import ApiHTTPException, ApiResponse, error_responses
from track_a_clinical import audit
from track_a_clinical.api.dependencies import get_db_session, get_redis
from track_a_clinical.api.schemas import (
    EndSessionData,
    StartSessionData,
    StartSessionRequest,
)
from track_a_clinical.config import get_settings
from track_a_clinical.models import (
    ENCOUNTER_STATUS_ACTIVE,
    ENCOUNTER_STATUS_COMPLETED,
    Encounter,
)
from track_a_clinical.session_tokens import mint_session_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])

ERROR_CODE_SESSION_NOT_FOUND = "session_not_found"
ERROR_CODE_SIGNAL_NOT_PUBLISHED = "signal_not_published"

#: What these statuses mean on *these* routes. api_envelope carries generic
#: wording for each; a 404 here is more specific than "the resource does not
#: exist", and the published spec is better for saying so.
SESSION_ERROR_DESCRIPTIONS = {
    status.HTTP_404_NOT_FOUND: "The session is unknown or its encounter is soft-deleted.",
    status.HTTP_503_SERVICE_UNAVAILABLE: (
        "The session ended but its signal could not be published."
    ),
}


def session_ended_channel(session_id: uuid.UUID) -> str:
    """Return the Redis channel for a session's end signal."""
    return f"session:ended:{session_id}"


def _client_ip(request: Request) -> str | None:
    """Return the requesting client's IP, or None when the transport has no peer."""
    return request.client.host if request.client else None


@router.post(
    "/start",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[StartSessionData],
    summary="Start a session",
    response_description="The new session's id and its short-lived JWT.",
    responses=error_responses(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=SESSION_ERROR_DESCRIPTIONS,
    ),
)
async def start_session(
    body: StartSessionRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[StartSessionData]:
    """Open an encounter and mint its session JWT.

    Creates an ``encounters`` row with ``status='active'`` and returns a token
    carrying ``{session_id, provider_id, exp}``, valid for ``SESSION_TTL_SECONDS``
    (900 by default). The client passes that token to the audio and nudge
    WebSockets; this is the only endpoint that issues one.

    Returns 201 with the session id and token. The encounter row and its audit
    record commit together — neither exists without the other.
    """
    # Minted before the row is written so a misconfigured signing key fails the
    # request outright rather than leaving an encounter nothing can connect to.
    session_id = uuid.uuid4()
    token = mint_session_jwt(
        session_id=session_id,
        provider_id=body.provider_id,
        settings=get_settings(),
    )

    encounter = Encounter(
        session_id=session_id,
        patient_fhir_id=body.patient_id,
        provider_id=body.provider_id,
        ehr_encounter_id=body.ehr_encounter_id,
        status=ENCOUNTER_STATUS_ACTIVE,
    )
    session.add(encounter)
    # Flush rather than commit: the primary key is server-generated and the audit
    # row needs it, but both writes still have to land in one transaction.
    await session.flush()

    await audit.audit_encounter_access(
        session,
        action=audit.ACTION_START_SESSION,
        encounter_id=encounter.id,
        session_id=session_id,
        provider_id=body.provider_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()

    return ApiResponse[StartSessionData](data=StartSessionData(session_id=session_id, jwt=token))


@router.post(
    "/{session_id}/end",
    response_model=ApiResponse[EndSessionData],
    summary="End a session",
    response_description="The session's final status and end time.",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        descriptions=SESSION_ERROR_DESCRIPTIONS,
    ),
)
async def end_session(
    session_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ApiResponse[EndSessionData]:
    """Close an encounter and signal the services waiting on it.

    Sets ``status='completed'`` and ``ended_at``, then publishes an empty message
    to ``session:ended:{session_id}``.

    Idempotent: a repeat call on an already-completed session returns 200 and
    publishes nothing. TASK-030 and TASK-060 both act on that signal, so a second
    publish would generate a second SOAP note and a second prior-auth bundle for
    one visit. Returns 404 when the session is unknown or its encounter has been
    soft-deleted.
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
            f"No active encounter for session {session_id}",
        )

    already_ended = encounter.status == ENCOUNTER_STATUS_COMPLETED
    if not already_ended:
        encounter.status = ENCOUNTER_STATUS_COMPLETED
        encounter.ended_at = sa.func.now()

    await audit.audit_encounter_access(
        session,
        action=audit.ACTION_READ_ENCOUNTER if already_ended else audit.ACTION_END_SESSION,
        encounter_id=encounter.id,
        session_id=session_id,
        provider_id=encounter.provider_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    if not already_ended:
        # ended_at was written as NOW() so every service shares one clock; the
        # value has to be read back before it can be returned.
        await session.refresh(encounter)
        await _publish_session_ended(redis, session_id)

    return ApiResponse[EndSessionData](
        data=EndSessionData(
            session_id=session_id,
            status=encounter.status,
            # started_at only stands in for a row whose status was set outside
            # this endpoint; the transition above always writes ended_at.
            ended_at=encounter.ended_at or encounter.started_at,
            already_ended=already_ended,
        )
    )


async def _publish_session_ended(redis: Redis, session_id: uuid.UUID) -> None:
    """Publish the empty end-of-session signal, or fail the request loudly.

    The publish follows the commit, so a broker failure leaves an encounter
    correctly marked completed with no signal sent, and the idempotent retry
    above will not re-send it. Raising 503 makes that visible to the caller and
    to alerting instead of silently stranding TASK-030 and TASK-060; replaying
    the signal is an operator action until a durable outbox exists.
    """
    try:
        await redis.publish(session_ended_channel(session_id), "")
    except RedisError:
        logger.exception("Failed to publish session-ended signal for %s", session_id)
        raise ApiHTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ERROR_CODE_SIGNAL_NOT_PUBLISHED,
            f"Session {session_id} was ended but its signal could not be published",
        ) from None

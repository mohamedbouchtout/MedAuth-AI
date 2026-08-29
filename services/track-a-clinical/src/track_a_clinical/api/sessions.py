"""Session lifecycle endpoints — the issuer every real-time service depends on.

``POST /sessions/start`` creates the ``encounters`` row, mints the session JWT
that ``audio-ingestion`` (TASK-020) and ``nudge-service`` (TASK-041) validate,
and announces the new session on ``sessions:started``.
``POST /sessions/{session_id}/end`` closes the encounter and publishes the
``session:ended:{session_id}`` signal that SOAP generation (TASK-030) and prior
auth bundle assembly (TASK-060) both subscribe to.
``POST /sessions/{session_id}/token`` (TASK-006b) re-mints the JWT for a visit
already under way, so a session outliving its 15-minute token does not have to
be started again — which would fork one encounter into two.

**Why start is announced at all** (added in TASK-021): a service that consumes
``transcription:{session_id}`` has to learn the session id from somewhere before
it can subscribe to that exact channel. The alternative was to pattern-subscribe
``transcription:*``, which puts a wildcard on the one channel family carrying
PHI and makes every consumer see every session. So this endpoint publishes the
id on a single fixed channel and consumers subscribe per session. It is
published *before* the response returns, and a client cannot open the audio
socket until it has the JWT from that response, so a consumer is always
subscribed before the first transcript segment can exist.

Both rows and both signals key off ``session_id``, not the encounter primary key:
that is the identifier already travelling through every Redis channel name in
CLAUDE.md's canonical list.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Final

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Header, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from api_envelope import ApiHTTPException, ApiResponse, error_responses
from hipaa_logger import AuditAction
from track_a_clinical import audit
from track_a_clinical.api.dependencies import get_db_session, get_redis
from track_a_clinical.api.schemas import (
    EndSessionData,
    RemintTokenData,
    StartSessionData,
    StartSessionRequest,
)
from track_a_clinical.config import get_settings
from track_a_clinical.models import (
    ENCOUNTER_STATUS_ACTIVE,
    ENCOUNTER_STATUS_COMPLETED,
    Encounter,
)
from track_a_clinical.session_tokens import (
    RemintCredentialError,
    mint_session_jwt,
    validate_remint_credential,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])

ERROR_CODE_SESSION_NOT_FOUND = "session_not_found"
ERROR_CODE_SIGNAL_NOT_PUBLISHED = "signal_not_published"
ERROR_CODE_AUTH_REJECTED = "auth_rejected"
ERROR_CODE_SESSION_COMPLETED = "session_completed"

_BEARER_PREFIX = "bearer "

#: What these statuses mean on *these* routes. api_envelope carries generic
#: wording for each; a 404 here is more specific than "the resource does not
#: exist", and the published spec is better for saying so.
SESSION_ERROR_DESCRIPTIONS = {
    status.HTTP_404_NOT_FOUND: "The session is unknown or its encounter is soft-deleted.",
    status.HTTP_503_SERVICE_UNAVAILABLE: (
        "The session ended but its signal could not be published."
    ),
}

#: The re-mint route's 401 and 409 both mean something a caller acts on
#: differently — retrying is pointless for either, but only one of them means the
#: visit is over — so neither is left on api_envelope's generic wording.
REMINT_ERROR_DESCRIPTIONS = {
    status.HTTP_401_UNAUTHORIZED: (
        "No session token was presented, or the one presented is invalid, is for "
        "a different session, or expired longer ago than the grace window allows."
    ),
    status.HTTP_404_NOT_FOUND: SESSION_ERROR_DESCRIPTIONS[status.HTTP_404_NOT_FOUND],
    status.HTTP_409_CONFLICT: (
        "The encounter is already completed. A finished visit cannot obtain a "
        "token, because that token could reopen its audio socket."
    ),
}

#: 503 means something different on each of these two routes, so the wording is
#: per route rather than shared — a spec that says "the session ended" on the
#: start endpoint would be actively misleading about what happened.
START_SESSION_ERROR_DESCRIPTIONS = {
    status.HTTP_503_SERVICE_UNAVAILABLE: (
        "The session was created but its start signal could not be published."
    ),
}


#: The one fixed channel in CLAUDE.md's Redis key list. Fixed rather than
#: per-session because a consumer that does not yet know a session exists cannot
#: name its channel; the id travels in the payload instead of the channel name.
SESSIONS_STARTED_CHANNEL: Final = "sessions:started"


def session_ended_channel(session_id: uuid.UUID) -> str:
    """Return the Redis channel for a session's end signal."""
    return f"session:ended:{session_id}"


def _client_ip(request: Request) -> str | None:
    """Return the requesting client's IP, or None when the transport has no peer."""
    return request.client.host if request.client else None


def _bearer_token(authorization: str | None) -> str:
    """Return the token from an ``Authorization: Bearer`` header, or empty string.

    An absent header and a malformed one are not distinguished: both end in the
    same 401, and telling a caller which it was changes nothing it can do.
    """
    if authorization and authorization.lower().startswith(_BEARER_PREFIX):
        return authorization[len(_BEARER_PREFIX) :].strip()
    return ""


@router.post(
    "/start",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[StartSessionData],
    summary="Start a session",
    response_description="The new session's id and its short-lived JWT.",
    responses=error_responses(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        descriptions=START_SESSION_ERROR_DESCRIPTIONS,
    ),
)
async def start_session(
    body: StartSessionRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ApiResponse[StartSessionData]:
    """Open an encounter and mint its session JWT.

    Creates an ``encounters`` row with ``status='active'`` and returns a token
    carrying ``{session_id, provider_id, exp}``, valid for ``SESSION_TTL_SECONDS``
    (900 by default). The client passes that token to the audio and nudge
    WebSockets; this is the only endpoint that issues one.

    Also publishes the new ``session_id`` to ``sessions:started``, which is how
    ``track-b-rag``'s transcript consumer (TASK-021) learns to subscribe to
    ``transcription:{session_id}``.

    Returns 201 with the session id and token. The encounter row and its audit
    record commit together — neither exists without the other. Returns 503 when
    the encounter was created but the start signal could not be published; see
    :func:`_publish_session_started` for why that is not downgraded to a log
    line.
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
        action=AuditAction.START_SESSION,
        encounter_id=encounter.id,
        session_id=session_id,
        provider_id=body.provider_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    await _publish_session_started(redis, session_id)

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
        action=AuditAction.READ_ENCOUNTER if already_ended else AuditAction.END_SESSION,
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


@router.post(
    "/{session_id}/token",
    response_model=ApiResponse[RemintTokenData],
    summary="Re-mint a session token",
    response_description="The same session's id and a freshly minted JWT.",
    responses=error_responses(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=REMINT_ERROR_DESCRIPTIONS,
    ),
)
async def remint_session_token(
    session_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> ApiResponse[RemintTokenData]:
    """Issue a fresh JWT for a session that is already under way.

    ``SESSION_TTL_SECONDS`` is 15 minutes and real orthopedic and dermatology
    visits routinely run longer. Validation is handshake-only, so a socket opened
    at minute 0 keeps streaming at minute 40 — expiry bites only when a *new*
    socket must be opened, on a reconnect or when the nudge socket opens later
    than the audio one. This endpoint is what a client calls then.

    **It is not a second ``/sessions/start``, and that distinction is the whole
    point.** Starting again would create a second ``encounters`` row with a new
    ``session_id``, forking one visit into two: the transcript splits across two
    ``transcription:{session_id}`` channels, TASK-030 writes two partial SOAP
    notes, TASK-060 assembles a bundle from whichever half it saw, and
    ``procedure_seen:{session_id}`` stops deduping so one procedure nudges twice.
    Nothing errors anywhere on that path, which is why this is an endpoint rather
    than a warning in a document.

    Returns 200 — nothing is created, unlike ``/sessions/start``'s 201. Writes no
    row beyond the audit trail, and publishes nothing: no consumer learns anything
    from a re-mint, and a second ``sessions:started`` would make TASK-021
    re-subscribe to a channel it already holds. A refreshed token does not extend
    the encounter; only ``POST /sessions/{session_id}/end`` ends it.

    Authorisation is the session's own token, presented as ``Authorization:
    Bearer``, expired or not — see
    :func:`~track_a_clinical.session_tokens.validate_remint_credential` for why
    that is the right strength here and what bounds it. Only the header carrier is
    accepted: the ``Sec-WebSocket-Protocol`` carrier exists because the native
    ``WebSocket`` constructor cannot set headers, and a plain POST can.

    Returns 401 when the presented token does not authorise this session, 404
    when the session is unknown or soft-deleted, and 409 when its encounter is
    already completed.
    """
    settings = get_settings()
    try:
        validate_remint_credential(
            _bearer_token(authorization), session_id=session_id, settings=settings
        )
    except RemintCredentialError as exc:
        # exc.reason names the kind of failure and never the token or a claim.
        logger.warning("Rejected re-mint for session %s: %s", session_id, exc.reason)
        raise ApiHTTPException(
            status.HTTP_401_UNAUTHORIZED,
            ERROR_CODE_AUTH_REJECTED,
            "The presented session token does not authorise a re-mint",
        ) from None

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
    if encounter.status == ENCOUNTER_STATUS_COMPLETED:
        raise ApiHTTPException(
            status.HTTP_409_CONFLICT,
            ERROR_CODE_SESSION_COMPLETED,
            f"Session {session_id} is already completed and cannot be re-minted",
        )

    # The provider comes from the row, never from the presented token's claim: the
    # row is what /sessions/start recorded, so a re-mint cannot alter the identity
    # the original token was issued for even if the token itself is odd.
    token = mint_session_jwt(
        session_id=session_id,
        provider_id=encounter.provider_id,
        settings=settings,
    )

    await audit.audit_encounter_access(
        session,
        action=AuditAction.REMINT_SESSION_TOKEN,
        encounter_id=encounter.id,
        session_id=session_id,
        provider_id=encounter.provider_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # Nothing above changed a row, so this commits the audit write alone — which
    # is the point: the encounter read is what has to be recorded.
    await session.commit()

    return ApiResponse[RemintTokenData](data=RemintTokenData(session_id=session_id, jwt=token))


async def _publish_session_started(redis: Redis, session_id: uuid.UUID) -> None:
    """Announce the new session, or fail the request loudly.

    The payload is ``{"session_id": ...}`` rather than empty, because the
    channel is fixed and so carries no id of its own. That is the one structural
    difference from the end signal.

    A failure here is a 503 for the same reason ending one is: a consumer that
    never hears about this session never subscribes to its transcript channel,
    so no procedure keyword is ever detected and no nudge is ever raised for the
    whole encounter. The provider would see a working visit with no alerts,
    which is indistinguishable from a visit with nothing to alert about —
    exactly the "silence reads as no authorization concern" failure this
    pipeline is built to avoid. Better to refuse the session and let the client
    retry than to run one that is quietly half-wired.

    The cost of that choice is a committed encounter row nobody uses, since the
    retry mints a new session. An orphaned ``active`` encounter is inert; a
    silently unwatched live one is not.
    """
    try:
        await redis.publish(SESSIONS_STARTED_CHANNEL, json.dumps({"session_id": str(session_id)}))
    except RedisError:
        logger.exception("Failed to publish session-started signal for %s", session_id)
        raise ApiHTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ERROR_CODE_SIGNAL_NOT_PUBLISHED,
            f"Session {session_id} was created but its start signal could not be published",
        ) from None


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

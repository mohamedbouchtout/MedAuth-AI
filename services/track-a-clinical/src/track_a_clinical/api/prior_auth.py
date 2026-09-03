"""Prior-authorization endpoints — the server side of a submission (TASK-054).

``GET /prior-auth/{request_id}`` returns what a submission needs to be built
from; ``PATCH /prior-auth/{request_id}/submission`` records what the payer said.
Both exist because the work is split across two services: ``fhir-integration``
holds the EHR credential and the adapter that speaks Da Vinci PAS, and this
service owns ``prior_auth_requests`` and the ``encounters`` row behind it.

**Server-to-server, and no browser calls either of them.** They are the
prior-auth counterpart of ``/notes/{session_id}/ehr-reference``, and the reason
is the same one CLAUDE.md gives under "Writing clinical data out to the EHR":
the submitting service reading this over HTTP is what makes the read produce a
``READ_PRIOR_AUTH`` row, where a direct database connection from that service —
which it has deliberately never had — would produce none.

**Keyed on the request's own primary key rather than on a session.** One
encounter can carry several prior-authorization requests, so a ``session_id``
would not name one. This is the arrangement
``PATCH /nudges/{nudge_id}/acknowledge`` already settled: a route takes the
identifier its caller was handed. See CLAUDE.md, "A route keyed on a resource
rather than a session follows the same v1 rule" — which also settles the
credential question these routes inherit unchanged: none in v1, and the actor
comes from the ``encounters`` row rather than from anything the caller sent.

**Everything here is PHI**, and ``clinical_evidence`` is transcript text. Both
routes audit, and no log line in this module carries a procedure, a diagnosis or
an excerpt.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from api_envelope import ApiHTTPException, ApiResponse, error_responses
from hipaa_logger import AuditAction
from track_a_clinical import audit, prior_auth
from track_a_clinical.api.dependencies import get_db_session
from track_a_clinical.api.schemas import PriorAuthRequestData, RecordSubmissionRequest
from track_a_clinical.models import Encounter, PriorAuthRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/prior-auth", tags=["prior-auth"])

ERROR_CODE_REQUEST_NOT_FOUND = "prior_auth_request_not_found"
#: Refusing a second submission rather than recording it. A payer that receives
#: one request twice may open two reviews, and the second reference number would
#: overwrite the first here with nothing recording that two exist.
ERROR_CODE_ALREADY_SUBMITTED = "prior_auth_already_submitted"

REQUEST_ERROR_DESCRIPTIONS = {
    status.HTTP_404_NOT_FOUND: (
        "No such prior-authorization request, or its encounter has been "
        "soft-deleted (`prior_auth_request_not_found`)."
    ),
}


def _client_ip(request: Request) -> str | None:
    """Return the requesting client's IP, or None when the transport has no peer."""
    return request.client.host if request.client else None


async def _load(session: AsyncSession, request_id: uuid.UUID) -> tuple[PriorAuthRequest, Encounter]:
    """Resolve a request id to its row and that row's encounter.

    Raises:
        ApiHTTPException: 404 when no such request exists.
    """
    found = await prior_auth.load_request(session, request_id)
    if found is None:
        raise ApiHTTPException(
            status.HTTP_404_NOT_FOUND,
            ERROR_CODE_REQUEST_NOT_FOUND,
            f"No prior authorization request {request_id}",
        )
    return found


@router.get(
    "/{request_id}",
    response_model=ApiResponse[PriorAuthRequestData],
    summary="Read a prior-authorization request for submission",
    response_description="What a payer submission is built from, and whether one has happened.",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=REQUEST_ERROR_DESCRIPTIONS,
    ),
)
async def read_prior_auth_request(
    request_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[PriorAuthRequestData]:
    """Return one prior-authorization request, with the EHR identifiers it needs.

    Answers the questions ``fhir-integration`` cannot answer for itself: what is
    being requested, of which payer, on whose behalf, and — the one that decides
    whether it may act at all — whether this request has already been submitted.

    A null ``ehr_encounter_id`` is an ordinary answer rather than an error here.
    The visit was started outside a SMART launch, and the caller decides what to
    do about it.

    This returns transcript excerpts and a patient identifier, so it is a PHI
    read and writes a ``READ_PRIOR_AUTH`` row against the request.
    """
    prior_auth_request, encounter = await _load(session, request_id)

    await audit.audit_prior_auth_access(
        session,
        action=AuditAction.READ_PRIOR_AUTH,
        request_id=prior_auth_request.id,
        session_id=encounter.session_id,
        provider_id=encounter.provider_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()

    return ApiResponse[PriorAuthRequestData](
        data=PriorAuthRequestData.from_rows(request=prior_auth_request, encounter=encounter)
    )


@router.patch(
    "/{request_id}/submission",
    response_model=ApiResponse[PriorAuthRequestData],
    summary="Record a submission and what the payer said",
    response_description="The request, now carrying the payer's answer.",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=REQUEST_ERROR_DESCRIPTIONS
        | {
            status.HTTP_409_CONFLICT: (
                "This request has already been submitted "
                "(`prior_auth_already_submitted`). Refused rather than recorded "
                "twice: a payer that receives one request twice may open two "
                "reviews, and only one reference number can be kept here."
            ),
        },
    ),
)
async def record_prior_auth_submission(
    request_id: uuid.UUID,
    body: RecordSubmissionRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[PriorAuthRequestData]:
    """Record that this request was transmitted to a payer, and what came back.

    **This is the only writer of ``submission_method``, ``payer_outcome`` and
    ``payer_reference_number``.** It is called by ``fhir-integration`` after a
    payer has actually answered, which is what makes the values trustworthy.

    **The payer's answer is recorded even when it is a refusal.** An ``error``
    outcome leaves ``status`` at ``error`` rather than ``submitted``: nothing is
    pending with the payer, and recording it as submitted would leave someone
    waiting for a decision on a request that was never taken in.

    **Write-once, enforced by the update's own ``WHERE`` clause** rather than by
    a preceding read, so two concurrent submissions cannot both be recorded. The
    caller that loses learns it lost.

    The actor is the encounter's provider, never the calling service.
    """
    prior_auth_request, encounter = await _load(session, request_id)

    recorded = await prior_auth.record_submission(
        session,
        request=prior_auth_request,
        encounter=encounter,
        submission_method=body.submission_method,
        outcome=body.outcome,
        payer_reference_number=body.payer_reference_number,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if recorded is None:
        raise ApiHTTPException(
            status.HTTP_409_CONFLICT,
            ERROR_CODE_ALREADY_SUBMITTED,
            f"Prior authorization request {request_id} has already been submitted",
        )

    return ApiResponse[PriorAuthRequestData](
        data=PriorAuthRequestData.from_rows(request=recorded, encounter=encounter)
    )

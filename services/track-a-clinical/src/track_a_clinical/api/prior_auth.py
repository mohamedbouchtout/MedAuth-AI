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

**Everything here is PHI**, and ``clinical_evidence`` is clinical
documentation drawn from the provider's note. Both routes audit, and no log line
in this module carries a procedure, a diagnosis or an excerpt.
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
from track_a_clinical.api.schemas import (
    PriorAuthRequestData,
    PriorAuthRoutingData,
    RecordSubmissionRequest,
)
from track_a_clinical.models import Encounter, PriorAuthRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/prior-auth", tags=["prior-auth"])

ERROR_CODE_REQUEST_NOT_FOUND = "prior_auth_request_not_found"
#: Refusing to submit a request the payer is still holding. A payer that
#: receives one request twice may open two reviews of it.
#:
#: Since TASK-061 this is narrower than its name suggests, and the name is kept
#: because it is a published error code that ``fhir-integration`` matches on: a
#: request the payer *denied* or *refused to take in* is resubmittable, and that
#: writes a new ``prior_auth_submission_attempts`` row rather than answering
#: this. What is refused is a repeat of a live request.
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

    This returns note excerpts and a patient identifier, so it is a PHI
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


@router.get(
    "/{request_id}/routing",
    response_model=ApiResponse[PriorAuthRoutingData],
    summary="Read what a submission path is chosen from",
    response_description="The payer, the launch, and whether the request may be sent.",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=REQUEST_ERROR_DESCRIPTIONS,
    ),
)
async def read_prior_auth_routing(
    request_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[PriorAuthRoutingData]:
    """Return the three facts a submission router chooses a path from.

    Which payer, whether the request may be sent, and which SMART launch holds
    the credential — and nothing else. TASK-061's router in ``prior-auth`` is the
    caller.

    **This route writes no audit row, and that is the rule rather than an
    exception to it.** CLAUDE.md's constraint is an "if and only if": a route
    audits when it touches PHI, and a route over non-clinical data must *not*,
    because the audit table's value comes from every row in it being a PHI
    access. The response carries no patient identifier, no procedure, no
    diagnosis and no note excerpt. Reading it through
    ``GET /prior-auth/{request_id}`` instead — which does audit, correctly, since
    it returns clinical evidence — would have written a ``READ_PRIOR_AUTH`` row
    for an access that read no clinical content, and pulled note excerpts across
    the network for a decision that never looks at them.

    **So do not add a clinical field to this payload.** The moment one appears
    the route owes an audit row, and the two would then disagree silently. A
    caller that needs clinical content wants the full route.

    Takes no session token in v1, on the same terms as every other route here.
    """
    prior_auth_request, encounter = await _load(session, request_id)
    return ApiResponse[PriorAuthRoutingData](
        data=PriorAuthRoutingData.from_rows(request=prior_auth_request, encounter=encounter)
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
                "This request is not in a state that may be submitted "
                "(`prior_auth_already_submitted`) — the payer is still holding "
                "it. Refused rather than recorded twice: a payer that receives "
                "one request twice may open two reviews. A `denied` or `error` "
                "request may be resubmitted, and is recorded as a new attempt."
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

    **Each attempt is write-once, enforced by the update's own ``WHERE`` clause**
    rather than by a preceding read, so two concurrent submissions cannot both be
    recorded. The caller that loses learns it lost.

    **A deliberate resubmission is a new attempt row, not a second write here.**
    A `denied` or `error` request may be sent again (TASK-072's flow); each
    transmission gets a ``prior_auth_submission_attempts`` row, and the columns
    on this row carry the latest attempt's result. A request the payer is still
    holding is refused exactly as it always was.

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
            f"Prior authorization request {request_id} is not in a submittable state",
        )

    return ApiResponse[PriorAuthRequestData](
        data=PriorAuthRequestData.from_rows(request=recorded, encounter=encounter)
    )

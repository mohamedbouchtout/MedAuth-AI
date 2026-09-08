"""``POST /prior-auth/{request_id}/submit`` — the submission router. TASK-061.

This service's first real route: everything before it arrived on a Redis
subscription. The routing model it implements is written out in
:mod:`prior_auth.router`; this module is the HTTP surface over it, and is
deliberately thin.

**The body is empty and the path names the request.** Everything a submission
needs is resolved server-side, exactly as ``POST /fhir/prior-auth`` and
``POST /fhir/notes`` take only an identifier: a client posting the bundle back
would put a payer submission's payload under the control of the least trusted
participant in it.

**Browser-facing.** TASK-072's dashboard calls this to resubmit a denied
request, which is why this service installs CORS from
``packages/cors-policy``.

**No credential in v1**, on the same terms as every other browser-facing route
in this repository: ``POST /sessions/start`` accepts ``provider_id`` as an
unauthenticated body field, and no provider-authentication mechanism exists
before SMART on FHIR in Phase 5. See CLAUDE.md, "Session-scoped routes are keyed
on ``session_id``".

**This route writes no audit row of its own, and that is the rule rather than an
omission.** It reads no clinical content — the routing read it makes is the
narrow one, which carries no patient identifier, no procedure and no note
excerpt. The PHI accesses that a submission does involve are audited where they
happen: ``fhir-integration`` writes ``SUBMIT_PRIOR_AUTH`` for the disclosure to
the payer, and ``track-a-clinical`` writes its own for the row it mutates. One
outbound write, one row per service that actually touched PHI — adding a third
here for a service that routed would make the trail say an access happened that
did not.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api_envelope import ApiHTTPException, ApiResponse, error_responses
from prior_auth import router as routing
from prior_auth import submission_client
from prior_auth.api.dependencies import get_db_session
from prior_auth.config import get_settings

router = APIRouter(prefix="/prior-auth", tags=["prior-auth"])

ERROR_CODE_REQUEST_NOT_FOUND = "prior_auth_request_not_found"
#: The request is not in a state that may be submitted — the payer is still
#: holding it. Decided by the service that owns the row, relayed here.
ERROR_CODE_NOT_SUBMITTABLE = "prior_auth_not_submittable"
#: The submitter refused for a reason that is not an unconfigured path — a
#: request it could not make conformant, or a launch that has expired. A person
#: has to look at it; retrying will not help.
ERROR_CODE_SUBMISSION_REFUSED = "prior_auth_submission_refused"
#: An upstream could not be reached. Transient, and retrying is reasonable —
#: **except** where the message says the payer may already hold the request.
ERROR_CODE_UPSTREAM_UNAVAILABLE = "prior_auth_upstream_unavailable"


class SubmissionRoutedData(BaseModel):
    """What the router did with the request.

    Attributes:
        request_id: The request that was routed.
        outcome: ``submitted`` when it was transmitted and a payer answered,
            ``manual-submission-required`` when no automated path exists. **The
            second is not an error** — most commercial plans are outside the
            CMS-0057-F mandate — and a client must render it as work for a
            person rather than as a failure or as a pending payer decision.
        payer_outcome: What the payer said, when it was asked. Null on the manual
            path. Distinct from ``outcome``: this is the payer's answer, that is
            ours.
        submission_method: Which path transmitted it, as the adapter reported.
            Null on the manual path, because nothing transmitted anything.
        payer_reference_number: The payer's reference, when it gave one.
            Legitimately absent on a queued answer.
        reason: Why a person has to submit this, on the manual path. A fixed
            label rather than prose, so a client can branch on it.
    """

    model_config = ConfigDict(frozen=True)

    request_id: uuid.UUID
    outcome: routing.RoutingOutcome = Field(
        description="What the router did — transmitted it, or handed it to a person."
    )
    payer_outcome: str | None = None
    submission_method: str | None = None
    payer_reference_number: str | None = None
    reason: str | None = None


@router.post(
    "/{request_id}/submit",
    response_model=ApiResponse[SubmissionRoutedData],
    status_code=status.HTTP_200_OK,
    summary="Submit a prior authorization down whichever path its payer supports",
    response_description="What the router did, and what the payer said if it was asked.",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_504_GATEWAY_TIMEOUT,
        descriptions={
            status.HTTP_404_NOT_FOUND: (
                "No such prior-authorization request (`prior_auth_request_not_found`)."
            ),
            status.HTTP_409_CONFLICT: (
                "The payer is still holding this request "
                "(`prior_auth_not_submittable`). A `denied` or `error` request "
                "may be resubmitted; one already with a payer may not."
            ),
            status.HTTP_422_UNPROCESSABLE_CONTENT: (
                "The path parameter is not a UUID, or the submitter refused the "
                "request (`prior_auth_submission_refused`) — it could not be "
                "made conformant, or the launch is no longer valid."
            ),
            status.HTTP_502_BAD_GATEWAY: (
                "An upstream could not be reached "
                "(`prior_auth_upstream_unavailable`). Nothing was transmitted "
                "unless the message says otherwise."
            ),
            status.HTTP_504_GATEWAY_TIMEOUT: (
                "An upstream did not answer in time. **A timed-out submission is "
                "ambiguous** — the payer may have taken the request in — so this "
                "is not retried automatically and should not be retried blindly."
            ),
        },
    ),
)
async def submit_prior_auth(
    request_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[SubmissionRoutedData]:
    """Route this request to a payer, or record that a person must submit it.

    **200 rather than 201**: nothing is created here. The request already exists,
    assembled by TASK-060; this transmits it, and the attempt row that records
    the transmission is written by the service that owns the table.

    The order is: read what the path is chosen from, choose, then act. Both
    outcomes are ordinary answers and neither is an error.
    """
    settings = get_settings()
    request_key = str(request_id)

    try:
        facts = await submission_client.read_routing_facts(
            request_key,
            base_url=settings.track_a_clinical_url,
            timeout_seconds=settings.track_a_clinical_timeout_seconds,
        )
    except submission_client.RoutingReadError as exc:
        if exc.not_found:
            raise ApiHTTPException(
                status.HTTP_404_NOT_FOUND,
                ERROR_CODE_REQUEST_NOT_FOUND,
                f"No prior authorization request {request_id}",
            ) from exc
        raise ApiHTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT if exc.timed_out else status.HTTP_502_BAD_GATEWAY,
            ERROR_CODE_UPSTREAM_UNAVAILABLE,
            "The clinical service could not be reached. Nothing was submitted.",
        ) from exc

    # Checked before anything is chosen, so a request the payer is holding costs
    # no work. The owning service re-checks atomically when the result is
    # recorded — that is the guarantee; this is the courtesy.
    if not facts.submittable:
        raise ApiHTTPException(
            status.HTTP_409_CONFLICT,
            ERROR_CODE_NOT_SUBMITTABLE,
            (
                f"Prior authorization request {request_id} is not in a state that "
                "may be submitted; the payer is still holding it."
            ),
        )

    try:
        decision = await routing.route_submission(session, request_id=request_key, facts=facts)
    except submission_client.SubmissionRefused as exc:
        if exc.code == submission_client.ERROR_CODE_ALREADY_SUBMITTED:
            raise ApiHTTPException(
                status.HTTP_409_CONFLICT,
                ERROR_CODE_NOT_SUBMITTABLE,
                (
                    f"Prior authorization request {request_id} is not in a state "
                    "that may be submitted; the payer is still holding it."
                ),
            ) from exc
        raise ApiHTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_CODE_SUBMISSION_REFUSED,
            (
                "The submission was refused and nothing is with the payer. This "
                "needs a person rather than a retry."
            ),
        ) from exc
    except submission_client.SubmissionUnavailable as exc:
        raise ApiHTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT if exc.timed_out else status.HTTP_502_BAD_GATEWAY,
            ERROR_CODE_UPSTREAM_UNAVAILABLE,
            (
                "The submission did not complete. The payer may still have "
                "received it, so reconcile before submitting again."
                if exc.timed_out
                else "The submission service could not be reached. Nothing was transmitted."
            ),
        ) from exc

    result = decision.result
    return ApiResponse[SubmissionRoutedData](
        data=SubmissionRoutedData(
            request_id=request_id,
            outcome=decision.outcome,
            payer_outcome=result.outcome if result else None,
            submission_method=result.submission_method if result else None,
            payer_reference_number=result.payer_reference_number if result else None,
            reason=decision.reason,
        )
    )

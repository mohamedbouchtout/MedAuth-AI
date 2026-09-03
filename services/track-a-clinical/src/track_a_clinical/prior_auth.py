"""Reading a prior-authorization request, and recording what the payer said.

TASK-054. ``fhir-integration`` submits a request it did not assemble: it holds
the EHR credential and the adapter that speaks Da Vinci PAS, and this service
owns ``prior_auth_requests`` and the ``encounters`` row behind it. So the two
calls here are the server side of that split — one read to build the submission
from, one write to record its answer.

**This service opens the database; that one never does.** The alternative would
be ``fhir-integration`` connecting to Postgres directly, which is the connection
it has deliberately never had, and the read would then produce no
``READ_PRIOR_AUTH`` row anywhere. Same arrangement, and same argument, as the
note write-back one task earlier.

Everything here is PHI: ``clinical_evidence`` holds transcript excerpts. Nothing
in this module logs a procedure, a diagnosis or an excerpt — only identifiers.
"""

from __future__ import annotations

import datetime
import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from hipaa_logger import AuditAction
from track_a_clinical import audit
from track_a_clinical.models import (
    PRIOR_AUTH_STATUS_ERROR,
    PRIOR_AUTH_STATUS_SUBMITTED,
    Encounter,
    PriorAuthRequest,
    SubmissionMethod,
    SubmissionOutcome,
)

logger = logging.getLogger(__name__)


def status_for_outcome(outcome: SubmissionOutcome) -> str:
    """Return the lifecycle status a payer's answer puts the request into.

    Three of the four leave it ``submitted``: the request is with the payer, and
    whether it has been decided is what ``payer_outcome`` records rather than
    this. ``error`` is the exception and the reason this function exists — the
    payer refused to process the request, so nothing is pending and nothing was
    authorized. Recording that as ``submitted`` is the specific failure TASK-054
    names: a caller would wait for a decision on a request the payer never took
    in.

    Args:
        outcome: What the payer said.

    Returns:
        The value for ``prior_auth_requests.status``.
    """
    if outcome is SubmissionOutcome.ERROR:
        return PRIOR_AUTH_STATUS_ERROR
    return PRIOR_AUTH_STATUS_SUBMITTED


async def load_request(
    session: AsyncSession, request_id: uuid.UUID
) -> tuple[PriorAuthRequest, Encounter] | None:
    """Load one prior-authorization request and the encounter it belongs to.

    Both rows, because neither answers the question alone: the request holds what
    is being asked for, and the encounter holds the two EHR identifiers and the
    payer columns that say who to ask and on whose behalf.

    Args:
        session: The active database session.
        request_id: The ``prior_auth_requests`` primary key.

    Returns:
        The request and its encounter, or None when there is no such request.
    """
    row = (
        await session.execute(
            sa.select(PriorAuthRequest, Encounter)
            .join(Encounter, PriorAuthRequest.encounter_id == Encounter.id)
            .where(
                PriorAuthRequest.id == request_id,
                Encounter.deleted_at.is_(None),
            )
        )
    ).one_or_none()

    if row is None:
        return None
    request, encounter = row.tuple()
    return request, encounter


async def record_submission(
    session: AsyncSession,
    *,
    request: PriorAuthRequest,
    encounter: Encounter,
    submission_method: SubmissionMethod,
    outcome: SubmissionOutcome,
    payer_reference_number: str | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> PriorAuthRequest | None:
    """Record that this request was transmitted, and what came back — once.

    **The guard is the ``WHERE`` clause, not a prior read**, exactly as it is for
    the note write-back and for the same reason: a caller that checks and then
    updates leaves a window in which a second caller does the same, and both
    submit. A duplicate prior authorization is not untidiness — a payer receiving
    one request twice may open two reviews, and the second reference number would
    overwrite the first here with nothing recording that two exist.

    Returning None is this function working rather than failing: somebody else
    submitted first, and the caller turns that into a 409.

    **``payer_reference_number`` may legitimately be ``None``.** A queued answer
    often carries no reference at all — ``ClaimResponse.preAuthRef`` is 0..1 and
    is only present on an adjudicated preauthorization — so its absence is not a
    failed submission and must not be treated as one.

    Args:
        session: The session whose transaction the update and its audit join.
        request: The row to record against, already loaded.
        encounter: The request's encounter — its ``provider_id`` is the audit
            actor, per the rule that an actor comes from the row rather than
            from the caller.
        submission_method: Which path transmitted it.
        outcome: What the payer said.
        payer_reference_number: The payer's reference, when it gave one.
        ip_address: Client IP, for the audit row.
        user_agent: Client user agent, for the audit row.

    Returns:
        The updated request, or None when it had already been submitted.
    """
    # Read before the statement runs: ``session.rollback()`` below expires every
    # instance in the session, and touching an attribute afterwards would lazy
    # load it — which raises MissingGreenlet in an async session and would turn
    # this ordinary refusal into a 500. Same trap as ``notes.record_ehr_document_ref``.
    row_id = request.id

    updated_id = await session.scalar(
        sa.update(PriorAuthRequest)
        .where(
            PriorAuthRequest.id == request.id,
            PriorAuthRequest.submitted_at.is_(None),
        )
        .values(
            status=status_for_outcome(outcome),
            submission_method=submission_method.value,
            payer_outcome=outcome.value,
            payer_reference_number=payer_reference_number,
            submitted_at=datetime.datetime.now(datetime.UTC),
        )
        .returning(PriorAuthRequest.id)
    )

    if updated_id is None:
        # Roll back rather than commit: nothing was written, and an audit row
        # claiming a request was submitted when this call submitted nothing
        # would be the same lie in the trail that a duplicate submission is at
        # the payer.
        await session.rollback()
        logger.info(
            "Prior auth request %s has already been submitted; this record was refused",
            row_id,
        )
        return None

    await audit.audit_prior_auth_access(
        session,
        action=AuditAction.SUBMIT_PRIOR_AUTH,
        request_id=row_id,
        session_id=encounter.session_id,
        provider_id=encounter.provider_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await session.commit()
    # The update was a Core statement, so the loaded object still carries the old
    # values, and ``expire_on_commit=False`` means the commit will not reload it
    # either. Refresh explicitly or the response would report the row as
    # unsubmitted on the one call that submitted it.
    await session.refresh(request)
    logger.info(
        "Recorded a %s submission for prior auth request %s on encounter %s",
        submission_method.value,
        row_id,
        encounter.id,
    )
    return request

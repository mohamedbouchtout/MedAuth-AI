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

Everything here is PHI: ``clinical_evidence`` holds excerpts of the provider's
note. Nothing in this module logs a procedure, a diagnosis or an excerpt — only
identifiers.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from hipaa_logger import AuditAction
from track_a_clinical import audit
from track_a_clinical.models import (
    PRIOR_AUTH_STATUS_DENIED,
    PRIOR_AUTH_STATUS_ERROR,
    PRIOR_AUTH_STATUS_SUBMITTED,
    Encounter,
    PriorAuthRequest,
    PriorAuthSubmissionAttempt,
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


#: The statuses a request may be resubmitted from. Both are terminal and
#: unsuccessful: ``denied`` means the payer decided against it, and ``error``
#: means the payer refused to take it in at all, so in neither case is anything
#: pending. Deliberately **not** ``submitted`` — a request the payer is still
#: holding must not be asked again, which is the whole point of the guard this
#: replaces.
RESUBMITTABLE_STATUSES: Final = frozenset(
    {
        PRIOR_AUTH_STATUS_DENIED,
        PRIOR_AUTH_STATUS_ERROR,
    }
)


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
    """Record that this request was transmitted, and what came back.

    Writes two things in one transaction: a new
    ``prior_auth_submission_attempts`` row holding *this* attempt's result, and
    the parent's own columns refreshed to the latest attempt. Both, because they
    answer different questions — the history, and the current state — and a
    reader of either would otherwise be told half the truth.

    **The guard is still a ``WHERE`` clause, not a prior read**, exactly as it
    was and for the same reason: a caller that checks and then updates leaves a
    window in which a second caller does the same, and both submit. What changed
    is the predicate. It used to be ``submitted_at IS NULL``, which made every
    repeat impossible including a deliberate one; it is now "never submitted, or
    sitting in a terminal unsuccessful state", so TASK-072 can resubmit a denied
    request while an accidental double-submit of a live one is refused exactly as
    before.

    That update is also what serialises concurrent callers: it takes the row's
    lock, so a second caller waits and then re-evaluates the predicate against
    what the first committed. The unique constraint on
    ``(request_id, attempt_number)`` is the backstop beneath it.

    Returning None is this function working rather than failing: the request was
    not in a state that may be submitted, and the caller turns that into a 409.

    **``payer_reference_number`` may legitimately be ``None``.** A queued answer
    often carries no reference at all — ``ClaimResponse.preAuthRef`` is 0..1 and
    is only present on an adjudicated preauthorization — so its absence is not a
    failed submission and must not be treated as one.

    Args:
        session: The session whose transaction the writes and their audit join.
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
        The updated request, or None when it was not in a submittable state.
    """
    # Read before the statement runs: ``session.rollback()`` below expires every
    # instance in the session, and touching an attribute afterwards would lazy
    # load it — which raises MissingGreenlet in an async session and would turn
    # this ordinary refusal into a 500. Same trap as ``notes.record_ehr_document_ref``.
    row_id = request.id
    submitted_at = datetime.datetime.now(datetime.UTC)

    updated_id = await session.scalar(
        sa.update(PriorAuthRequest)
        .where(
            PriorAuthRequest.id == request.id,
            sa.or_(
                PriorAuthRequest.submitted_at.is_(None),
                PriorAuthRequest.status.in_(RESUBMITTABLE_STATUSES),
            ),
        )
        .values(
            status=status_for_outcome(outcome),
            submission_method=submission_method.value,
            payer_outcome=outcome.value,
            payer_reference_number=payer_reference_number,
            submitted_at=submitted_at,
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
            "Prior auth request %s is not in a submittable state; this record was refused",
            row_id,
        )
        return None

    # Counted after the update above, so the row lock is already held and no
    # concurrent caller can be choosing the same number. The unique constraint
    # is what makes that a guarantee rather than an expectation.
    attempts_so_far = await session.scalar(
        sa.select(sa.func.count())
        .select_from(PriorAuthSubmissionAttempt)
        .where(PriorAuthSubmissionAttempt.request_id == row_id)
    )
    session.add(
        PriorAuthSubmissionAttempt(
            request_id=row_id,
            attempt_number=(attempts_so_far or 0) + 1,
            submission_method=submission_method.value,
            payer_outcome=outcome.value,
            payer_reference_number=payer_reference_number,
            submitted_at=submitted_at,
        )
    )

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

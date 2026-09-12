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

**TASK-072 added two reads that are not part of that split**, and they are the
reason this module is no longer server-to-server only: :func:`list_requests`
backs the provider's dashboard queue, and the decision fields it deliberately
omits are read one request at a time. A browser calls both.

Everything here is PHI *except the list*: ``clinical_evidence`` holds excerpts of
the provider's note, and ``denial_reason`` is a payer's account of why this
patient's care was refused. :func:`list_requests` is the exception by
construction rather than by luck — the route over it returns no clinical field,
which is what keeps it out of ``audit_log``, and that selection is stated at
:class:`~track_a_clinical.api.schemas.PriorAuthListItem` so a later field
addition has to check against it. Nothing in this module logs a procedure, a
diagnosis, an excerpt or a denial reason — only identifiers.
"""

from __future__ import annotations

import base64
import datetime
import logging
import uuid
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
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


#: How many requests one page of the dashboard list carries. A page bound rather
#: than a tuning constant: it exists so that one request cannot ask for every
#: prior authorization a provider has ever filed, and the cursor below is how a
#: caller that genuinely wants them all gets them.
LIST_PAGE_SIZE: Final = 50


class InvalidCursor(ValueError):
    """The caller sent a `cursor` this service did not issue.

    Raised rather than ignored. Silently restarting from the first page would
    make a broken pager look like a working one that had run out of rows, which
    is the same class of failure as an empty retrieval indistinguishable from a
    payer we hold no policy for.
    """


def encode_cursor(started_at: datetime.datetime, request_id: uuid.UUID) -> str:
    """Return the opaque cursor naming the last row of a page.

    Both halves of the sort key, because ``started_at`` alone does not identify a
    row: two visits can begin in the same microsecond, and a cursor that named
    only the timestamp would either skip the second one or serve it twice.

    Args:
        started_at: The encounter start time of the page's last row.
        request_id: That row's primary key.

    Returns:
        A base64 string carrying both values. Opaque to the caller by
        construction — it is ours to change, and nothing outside this module
        parses it.
    """
    raw = f"{started_at.isoformat()}|{request_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime.datetime, uuid.UUID]:
    """Return the sort key a cursor names.

    Args:
        cursor: A value produced by :func:`encode_cursor`.

    Returns:
        The encounter start time and request id to resume after.

    Raises:
        InvalidCursor: The value is not one this service issued.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        started_at_text, _, request_id_text = raw.partition("|")
        return datetime.datetime.fromisoformat(started_at_text), uuid.UUID(request_id_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor(f"Not a cursor this service issued: {cursor!r}") from exc


async def list_requests(
    session: AsyncSession,
    *,
    provider_id: uuid.UUID,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = LIST_PAGE_SIZE,
) -> tuple[list[tuple[PriorAuthRequest, Encounter]], str | None]:
    """Return one page of a provider's prior-authorization requests, newest first.

    **Scoped to one provider, and the scope is not optional.** The caller cannot
    ask for every request across every patient: that is a materially wider
    disclosure than the single-resource routes around it, and TASK-072 requires
    the narrower bar even though no route here takes a credential in v1. The
    provider is matched against the *encounter's* ``provider_id``, which is the
    same column every actor in this service is resolved from.

    **The page carries whole rows and the route narrows them.** Nothing clinical
    reaches the response — see :class:`~track_a_clinical.api.schemas.PriorAuthListItem`,
    which fixes the field list and explains why that selection is what keeps the
    route out of ``audit_log``.

    Ordered by the encounter's ``started_at`` descending with the request id as a
    tiebreak. ``prior_auth_requests`` carries no timestamp of its own, and the
    visit's start is the more meaningful date regardless: it is when the
    encounter this request came out of happened.

    Args:
        session: The active database session.
        provider_id: Whose requests to return.
        status: An exact ``prior_auth_requests.status`` to filter on, or None for
            every status.
        cursor: Resume after the row this names, or None for the first page.
        limit: Maximum rows in the page.

    Returns:
        The page's rows, and the cursor for the next page — None when this page
        is the last one.

    Raises:
        InvalidCursor: The cursor is not one this service issued.
    """
    query = (
        sa.select(PriorAuthRequest, Encounter)
        .join(Encounter, PriorAuthRequest.encounter_id == Encounter.id)
        .where(
            Encounter.provider_id == provider_id,
            Encounter.deleted_at.is_(None),
        )
        .order_by(Encounter.started_at.desc(), PriorAuthRequest.id.desc())
        # One more than the page, so "is there a next page" is answered by what
        # came back rather than by a second COUNT query over the same predicate.
        .limit(limit + 1)
    )

    if status is not None:
        query = query.where(PriorAuthRequest.status == status)

    if cursor is not None:
        last_started_at, last_request_id = decode_cursor(cursor)
        # Row-wise comparison against the composite sort key: strictly older
        # visits, plus the ties broken after the id we stopped at. Written as a
        # tuple so the two halves cannot drift from the ORDER BY above.
        query = query.where(
            sa.tuple_(Encounter.started_at, PriorAuthRequest.id)
            < sa.tuple_(
                sa.literal(last_started_at, sa.TIMESTAMP(timezone=True)),
                sa.literal(last_request_id, postgresql.UUID(as_uuid=True)),
            )
        )

    rows = [row.tuple() for row in (await session.execute(query)).all()]

    if len(rows) <= limit:
        return rows, None

    page = rows[:limit]
    last_request, last_encounter = page[-1]
    return page, encode_cursor(last_encounter.started_at, last_request.id)


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


def is_submittable(request: PriorAuthRequest) -> bool:
    """Return whether this request may be sent to a payer now.

    **The one definition of the rule**, so that no caller re-derives it. Before
    TASK-061 the rule was "``submitted_at`` is NULL" and every caller could spell
    it for itself; now it is "never submitted, or terminal and unsuccessful", and
    a caller still checking the old way refuses every legitimate resubmission.
    ``fhir-integration`` did exactly that, which is why its payload now carries
    this as a computed flag rather than the raw timestamp to reason from.

    It is deliberately the same predicate :func:`record_submission`'s ``WHERE``
    clause applies. This one answers in advance and can be raced; that one is the
    guarantee. Both come from :data:`RESUBMITTABLE_STATUSES`.

    Args:
        request: The row to judge.

    Returns:
        True when a submission may be attempted.
    """
    return request.submitted_at is None or request.status in RESUBMITTABLE_STATUSES


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

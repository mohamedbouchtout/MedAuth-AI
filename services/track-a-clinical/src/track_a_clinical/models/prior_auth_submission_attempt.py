"""``prior_auth_submission_attempts`` — one row per transmission to a payer.

TASK-061. A prior-authorization request can legitimately be sent more than once:
a payer denies it, the provider adds the documentation that was missing, and the
request goes again. TASK-072's dashboard is built on exactly that flow.

**Before this table, a second submission was impossible, and relaxing the thing
that made it impossible would have been the wrong fix.**
``prior_auth_requests.submitted_at`` being non-null is the atomic guard that
stops one request reaching a payer twice by accident — a payer that receives one
request twice may open two reviews of it — and that guard is the reason
``uq_prior_auth_requests_encounter`` and TASK-054's pre-check exist. Loosening it
to permit a deliberate resubmission would have reopened the accidental one, which
is a patient-visible failure rather than an untidiness.

So a resubmission is **a new row here**, never a second write to the first
attempt's result. What that buys, beyond keeping the guard:

* **The history is answerable.** "How many times was this payer asked, and what
  did it say each time" is the question a resubmission dashboard exists to
  answer, and a mutating column can never answer it — the second answer
  overwrites the first and nothing records that two exist. This is the same
  argument that made ``payer_outcome`` a column of its own rather than more
  values on ``status``.
* **The duplicate guard gets stronger, not weaker.** ``UNIQUE (request_id,
  attempt_number)`` refuses an accidental double-submit on the database rather
  than on a read-then-write, while admitting a deliberate retry. A racing pair
  produces one row and the loser learns it lost.

**The parent row keeps its outcome columns and they keep their meaning: the
latest attempt's result.** Nothing is deprecated and nothing migrates off them.
Every existing reader — ``GET /prior-auth?status=``, the submitter's
already-submitted pre-check — wants the current state rather than a history, and
making them join for it would be churn with no buyer. What changed is that
``prior_auth.record_submission`` now writes both, in one transaction.

Rows here carry no clinical content: a method, an outcome, a payer reference and
timestamps. The PHI stays on the parent.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from track_a_clinical.models.base import Base, timestamp_column, uuid_primary_key
from track_a_clinical.models.prior_auth_request import SubmissionMethod, SubmissionOutcome

if TYPE_CHECKING:
    from track_a_clinical.models.prior_auth_request import PriorAuthRequest


class PriorAuthSubmissionAttempt(Base):
    """One transmission of a prior-authorization request to a payer."""

    __tablename__ = "prior_auth_submission_attempts"
    __table_args__ = (
        sa.Index("idx_pa_attempts_request", "request_id"),
        # The duplicate guard, replacing ``submitted_at IS NULL`` as the thing
        # that makes an accidental repeat impossible. It admits attempt 2 and
        # refuses a second attempt 2, which is precisely the distinction
        # ``submitted_at`` could not draw.
        sa.UniqueConstraint(
            "request_id",
            "attempt_number",
            name="uq_pa_attempts_request_number",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_primary_key()
    request_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("prior_auth_requests.id"),
        nullable=False,
    )

    #: 1 for the first transmission, counting up. Dense and per-request rather
    #: than a bare timestamp ordering: the number is half of the unique
    #: constraint above, and two attempts written in the same millisecond would
    #: otherwise be indistinguishable.
    attempt_number: Mapped[int] = mapped_column(sa.Integer(), nullable=False)

    #: Which path transmitted this attempt, as one of :class:`SubmissionMethod`.
    #: Nullable because a row is shaped to hold an attempt that never reached a
    #: payer; every attempt written today carries one.
    submission_method: Mapped[str | None] = mapped_column(sa.String(50), nullable=True)

    #: What the payer said about *this* attempt, as one of
    #: :class:`SubmissionOutcome`.
    payer_outcome: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    payer_reference_number: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)

    submitted_at: Mapped[datetime.datetime | None] = timestamp_column(nullable=True)

    #: Why this attempt was denied, when something reads an adjudication.
    #: **Nothing writes this yet**, exactly as the parent's own ``denial_reason``
    #: is unwritten — following up a payer's decision is out of scope for every
    #: task built so far. It is here because a per-attempt reason is most of what
    #: a resubmission history is worth: attempt 1 denied for one reason, attempt
    #: 2 for another, is a story the parent column alone cannot tell.
    denial_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    created_at: Mapped[datetime.datetime] = timestamp_column(nullable=False, default_now=True)

    request: Mapped[PriorAuthRequest] = relationship(
        "PriorAuthRequest",
        back_populates="submission_attempts",
    )

    @validates("payer_outcome")
    def _validate_payer_outcome(self, _key: str, value: str | None) -> str | None:
        """Refuse a payer outcome outside :class:`SubmissionOutcome`.

        The same backstop, for the same reason, as the parent row's validator:
        this value arrives over HTTP from ``fhir-integration``, where the type
        system stopped applying, and an answer we cannot name is not an answer we
        can honestly record.
        """
        if value is None:
            return None
        try:
            return SubmissionOutcome(value).value
        except ValueError:
            permitted = ", ".join(sorted(outcome.value for outcome in SubmissionOutcome))
            raise ValueError(f"payer_outcome must be one of {permitted}; got {value!r}") from None

    @validates("submission_method")
    def _validate_submission_method(self, _key: str, value: str | None) -> str | None:
        """Refuse a submission method outside :class:`SubmissionMethod`."""
        if value is None:
            return None
        try:
            return SubmissionMethod(value).value
        except ValueError:
            permitted = ", ".join(sorted(method.value for method in SubmissionMethod))
            raise ValueError(
                f"submission_method must be one of {permitted}; got {value!r}"
            ) from None

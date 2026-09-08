"""Record each prior-authorization submission as its own attempt row.

TASK-061. TASK-072's dashboard resubmits a denied request, and until now that
was impossible: ``prior_auth_requests.submitted_at`` being non-null is what
makes a repeat refusable, and TASK-054's route and
``prior_auth.record_submission`` both refuse on it.

**Relaxing that guard would have been the wrong fix.** It is the same guard that
stops an accidental double-submit, and a payer that receives one request twice
may open two reviews of it — the failure ``uq_prior_auth_requests_encounter``
one migration earlier exists to prevent, arriving from the other direction. So a
deliberate resubmission becomes a new row here rather than a second write to the
first attempt's result, and ``UNIQUE (request_id, attempt_number)`` takes over as
the duplicate guard: it admits attempt 2 and refuses a second attempt 2, which is
exactly the distinction a single ``submitted_at`` could not draw.

The parent's outcome columns stay and keep meaning the latest attempt's result —
every existing reader wants the current state rather than a history.

**The backfill is what keeps the history honest.** Every request already carrying
a ``submitted_at`` gets an attempt 1 synthesised from its own columns, so a row
submitted before this migration has a first attempt rather than a history that
mysteriously begins at its second. Requests never submitted get no row, which is
the truthful answer: nothing was ever transmitted.

The revision id is abbreviated rather than spelled out because
``alembic_version`` holds ``VARCHAR(32)`` and the full name is 35 characters.
Alembic does not check that, so the spelled-out version imported cleanly, passed
every unit test, and failed only on ``INSERT INTO alembic_version`` against a
real database. Keep new ids under 32 characters.

Revision ID: 0009_pa_submission_attempts
Revises: 0008_prior_auth_per_encounter
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_pa_submission_attempts"
down_revision: str | None = "0008_prior_auth_per_encounter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the attempts table and backfill one row per submitted request."""
    op.create_table(
        "prior_auth_submission_attempts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prior_auth_requests.id"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("submission_method", sa.String(50), nullable=True),
        sa.Column("payer_outcome", sa.String(20), nullable=True),
        sa.Column("payer_reference_number", sa.String(200), nullable=True),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("denial_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "request_id",
            "attempt_number",
            name="uq_pa_attempts_request_number",
        ),
    )
    op.create_index(
        "idx_pa_attempts_request",
        "prior_auth_submission_attempts",
        ["request_id"],
    )

    # Attempt 1 for everything already transmitted. ``created_at`` is set to the
    # original submission time rather than to now: this row records something
    # that happened then, and stamping it with the migration's clock would make
    # the history date from the deployment instead of from the submission.
    op.execute(
        sa.text(
            """
            INSERT INTO prior_auth_submission_attempts (
                request_id,
                attempt_number,
                submission_method,
                payer_outcome,
                payer_reference_number,
                submitted_at,
                denial_reason,
                created_at
            )
            SELECT
                id,
                1,
                submission_method,
                payer_outcome,
                payer_reference_number,
                submitted_at,
                denial_reason,
                submitted_at
            FROM prior_auth_requests
            WHERE submitted_at IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    """Drop the table.

    The parent's own columns still carry the latest attempt's result, so this
    loses only the history of earlier attempts — which by construction did not
    exist before this migration.
    """
    op.drop_index("idx_pa_attempts_request", table_name="prior_auth_submission_attempts")
    op.drop_table("prior_auth_submission_attempts")

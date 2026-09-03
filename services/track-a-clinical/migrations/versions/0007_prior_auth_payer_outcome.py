"""Record what the payer said, separately from where the request has got to.

TASK-054 is the first code in this repository that submits anything to a payer,
and so the first that has an answer to record. ``prior_auth_requests`` had
nowhere honest to put one: ``status`` is our word for where a request has got to
in our own process, and the payer's answer is a different fact.

Collapsing the two would lose the distinction TASK-054 exists to preserve. A
payer that queued a request and one that adjudicated it are both ``submitted``
by our lifecycle, and are not the same thing to whoever follows one up; a payer
that *refused to process* the request is neither, and recording that as
``submitted`` would leave a caller waiting for a decision on a request the payer
never took in.

``VARCHAR(20)`` holding a ``SubmissionOutcome`` — ``complete``, ``queued``,
``partial`` or ``error``, the four-way required binding of
``ClaimResponse.outcome`` in the Da Vinci PAS IG, normalized so the CoverMyMeds
path answers in the same terms. Text with a model validator rather than a
database enum, exactly as ``submission_method`` is: adding a value stays a change
in one place, and the validator is what makes that safe.

Nullable, because a row that has not been submitted has no answer to hold, and
every existing row predates any submission at all.

Revision ID: 0007_prior_auth_payer_outcome
Revises: 0006_encounter_launch_id
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_prior_auth_payer_outcome"
down_revision: str | None = "0006_encounter_launch_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable payer_outcome column."""
    # No column comment, for the reason given in 0002: nothing else in this
    # schema carries one and the drift guard compares them.
    op.add_column("prior_auth_requests", sa.Column("payer_outcome", sa.String(20), nullable=True))


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("prior_auth_requests", "payer_outcome")

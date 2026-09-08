"""Enforce one prior-authorization bundle per encounter.

TASK-060's assembler is a Redis consumer on ``session:ended:{session_id}``, and
pub/sub delivery is not exactly-once. A redelivered signal, a consumer
reconnect, or a retry of the note-wait backoff would each insert a second
``prior_auth_requests`` row for one visit, and nothing anywhere would raise.

This is TASK-030's fix applied to the identical problem shape rather than a
second idempotency design — the same defensive shape as
``uq_clinical_notes_encounter`` one migration family over, and as
``_record_policy`` in track-b-rag before it. The consequence here is worse than
a duplicate note, which is why it gets at least the same treatment: TASK-061
submits from these rows, so a duplicate is a payer asked to open two reviews of
one request.

``DO NOTHING`` rather than ``DO UPDATE`` at the insert site, for TASK-030's
reason and one of its own: by the time a duplicate signal arrives the row may
already carry ``submission_method``, ``payer_reference_number``,
``submitted_at`` and ``payer_outcome``, and overwriting those would discard what
a payer actually said.

**TASK-090 is unaffected by this.** That task makes ``encounter_id`` nullable
and adds ``source``, so manually-submitted bundles carry no encounter at all.
Postgres treats NULLs as distinct in a unique index, so those rows neither
collide with each other nor with this constraint.

The redundant ``idx_prior_auth_encounter`` stays: TASK-005 specifies it by name
and ``test_upgrade_creates_every_index`` asserts the indexes it lists.

Revision ID: 0008_prior_auth_per_encounter
Revises: 0007_prior_auth_payer_outcome
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_prior_auth_per_encounter"
down_revision: str | None = "0007_prior_auth_payer_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the unique constraint on prior_auth_requests.encounter_id."""
    op.create_unique_constraint(
        "uq_prior_auth_requests_encounter",
        "prior_auth_requests",
        ["encounter_id"],
    )


def downgrade() -> None:
    """Drop the constraint."""
    op.drop_constraint(
        "uq_prior_auth_requests_encounter",
        "prior_auth_requests",
        type_="unique",
    )

"""Enforce one clinical note per encounter.

TASK-060 fetches a bundle's note by encounter and was written assuming at most
one exists. Nothing enforced that. The writer added in TASK-030 is a Redis
consumer, and pub/sub delivery is not exactly-once: a redelivered
``session:ended`` signal, a consumer reconnect, or a retry of a generation that
failed after its LLM calls would each insert a second row. Neither the duplicate
Sonnet call nor the duplicate row raises anything anywhere, and the bundle
assembler would then pick whichever row it saw first.

So the invariant becomes a constraint, and TASK-030's insert names it as an
``ON CONFLICT`` target — the same defensive shape as ``_record_policy`` in
track-b-rag, with ``DO NOTHING`` rather than ``DO UPDATE`` because the first
note generated for an encounter is the one to keep. A retry knows nothing the
attempt before it did not, and TASK-032's provider edits must not be quietly
replaced by a late duplicate signal.

The redundant ``idx_clinical_notes_encounter`` stays: TASK-005 specifies it by
name and a migration test asserts the seven indexes it lists.

Revision ID: 0004_clinical_note_per_encounter
Revises: 0003_encounter_state
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_clinical_note_per_encounter"
down_revision: str | None = "0003_encounter_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the unique constraint on clinical_notes.encounter_id."""
    op.create_unique_constraint(
        "uq_clinical_notes_encounter",
        "clinical_notes",
        ["encounter_id"],
    )


def downgrade() -> None:
    """Drop the constraint."""
    op.drop_constraint("uq_clinical_notes_encounter", "clinical_notes", type_="unique")

"""Enforce one nudge per procedure per encounter.

TASK-021's Redis-backed dedup claim already says a procedure named three times
in a visit raises one nudge, and TASK-041b's acknowledge endpoint assumes the
provider is dismissing *the* nudge for that procedure. Nothing enforced it.

The gap is TASK-040's retry path. The emitter writes the ``clinical_nudges``
row first, because the published payload carries its ``nudge_id`` and a client
must not be able to acknowledge a nudge that was never recorded. A publish that
fails after that write releases the dedup claim so a later mention gets another
attempt — correct for a query that failed, and an invitation to insert a second
row for the same procedure. Nothing would raise, and the provider would see the
same alert twice.

So the invariant becomes a constraint, and the emitter names it as an
``ON CONFLICT`` target — the same shape TASK-030 uses on ``clinical_notes``.

**Partial, and the ``WHERE`` is load-bearing.** TASK-044 raises nudges for
procedures that resolve no CPT code, where ``cpt_code`` is NULL. NULLs do not
collide in a unique index, so those rows are unconstrained by this one and the
keyword half of the invariant lands with the task that introduces the case.
Constraining them here would need the keyword persisted on the row, which is a
column TASK-044 should add when it has a use for it rather than one added
speculatively now.

The existing ``idx_clinical_nudges_encounter`` stays: TASK-005 specifies it by
name and a migration test asserts the seven indexes it lists.

Revision ID: 0005_one_nudge_per_procedure
Revises: 0004_clinical_note_per_encounter
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_one_nudge_per_procedure"
down_revision: str | None = "0004_clinical_note_per_encounter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_clinical_nudges_encounter_cpt"


def upgrade() -> None:
    """Add the partial unique index on (encounter_id, cpt_code)."""
    op.create_index(
        INDEX_NAME,
        "clinical_nudges",
        ["encounter_id", "cpt_code"],
        unique=True,
        postgresql_where=sa.text("cpt_code IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the index."""
    op.drop_index(INDEX_NAME, table_name="clinical_nudges")

"""Record which state an encounter takes place in.

``/policies/query`` is keyed on ``rag:{payer}:{plan_type}:{state}:{cpt_code}``
and its Qdrant filter matches ``state`` against the policy corpus, but nothing
on ``encounters`` said where the visit was. TASK-021 detects a procedure in a
live transcript and then cannot build a query, and this column is one of the
four parameters it is missing.

Nullable, because it is unknown until a SMART launch reads the patient's
address or the practice's own location (TASK-052b) — an encounter created by
``POST /sessions/start`` today has no source for it, and a NOT NULL column would
mean inventing one.

``CHAR(2)`` matching ``insurance_policies.state``, because the two are compared:
this is the query side of the same vocabulary TASK-013 normalises the ingestion
side into. CMS's sub-state jurisdiction codes are collapsed onto their parent
state before they reach either column, so ``CHAR(2)`` is wide enough for both —
the four-character ``CNMI`` becomes ``MP`` at ``payer_vocab.normalize_state``
and never reaches the database.

Revision ID: 0003_encounter_state
Revises: 0002_policy_jurisdiction_states
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_encounter_state"
down_revision: str | None = "0002_policy_jurisdiction_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable state column."""
    # No column comment, for the reason given in 0002: nothing else in this
    # schema carries one and the drift guard compares them.
    op.add_column("encounters", sa.Column("state", sa.CHAR(2), nullable=True))


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("encounters", "state")

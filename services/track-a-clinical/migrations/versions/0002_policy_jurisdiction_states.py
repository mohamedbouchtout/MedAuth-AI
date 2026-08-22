"""Record an ingested policy's jurisdiction, which is a set of states.

A Medicare local coverage determination is issued by a Medicare Administrative
Contractor and applies across that contractor's whole jurisdiction — a median of
twelve states across CMS's current export, up to forty-eight for the widest. The
existing single-valued ``state CHAR(2)`` cannot say that, and the alternative —
one row per state with a composite ``policy_id`` — would duplicate identical
policy text a dozen times over in Qdrant for no retrieval benefit.

So ``state`` keeps its meaning for genuinely single-state documents (the
commercial plan policies TASK-014 ingests), NULL on both columns still means a
national policy, and this column carries the jurisdiction when a document has
one. The Alembic history lives in this service because it owns migration
authorship for the shared schema, even though TASK-013's scraper is what writes
the column. See CLAUDE.md, "Migration Ownership vs. Table Write Access".

Revision ID: 0002_policy_jurisdiction_states
Revises: 0001_create_core_schema
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_policy_jurisdiction_states"
down_revision: str | None = "0001_create_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable jurisdiction column."""
    op.add_column(
        "insurance_policies",
        # No column comment: nothing else in this schema carries one, and the
        # drift guard compares them — a comment here and not on the mapped class
        # reads as a schema difference on every run.
        sa.Column("jurisdiction_states", postgresql.ARRAY(sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("insurance_policies", "jurisdiction_states")

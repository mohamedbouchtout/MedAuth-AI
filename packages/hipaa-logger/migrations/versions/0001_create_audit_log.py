"""Create the audit_log table.

Schema is authoritative per CLAUDE.md — it holds identifiers only, never PHI
content. There is no soft-delete column here on purpose: audit rows are append-only
and are never deleted or updated, which is the point of an audit trail.

Revision ID: 0001_create_audit_log
Revises:
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_create_audit_log"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create audit_log and its lookup indexes."""
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=True),
        sa.Column("resource_id", sa.String(length=200), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("service_name", sa.String(length=100), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("idx_audit_log_occurred_at", "audit_log", ["occurred_at"])
    op.create_index("idx_audit_log_actor", "audit_log", ["actor_id"])
    op.create_index("idx_audit_log_session", "audit_log", ["session_id"])


def downgrade() -> None:
    """Drop audit_log.

    Destructive by nature — dropping the audit trail is a compliance event, not a
    routine rollback. Intended for local development only.
    """
    op.drop_index("idx_audit_log_session", table_name="audit_log")
    op.drop_index("idx_audit_log_actor", table_name="audit_log")
    op.drop_index("idx_audit_log_occurred_at", table_name="audit_log")
    op.drop_table("audit_log")

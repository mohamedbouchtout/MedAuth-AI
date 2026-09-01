"""Add audit_log.fhir_practitioner_ref.

An actor this system minted is a UUID and lives in ``actor_id``. An actor an EHR
asserts is a FHIR ``Practitioner`` reference, whose id is ``[A-Za-z0-9\\-\\.]{1,64}``
and is usually not a UUID at all — so it cannot go in ``actor_id``, and widening
that column would weaken a guarantee every other row in this table depends on.
It gets its own column instead. See CLAUDE.md, "The EHR-asserted actor is its
own column".

Nullable, because almost every row will have one: only ``fhir-integration``'s
launch-time reads have an EHR-asserted actor, and even there an unverifiable
claim is written as NULL rather than on trust. The two actor columns are never
populated from each other and neither is a fallback for the other.

``VARCHAR(512)`` rather than ``resource_id``'s 200: the value stored is the
reference verbatim, normally an absolute URL, because a ``Practitioner`` id is
unique only within one EHR and the bare id would merge two servers' providers.

Revision ID: 0002_add_fhir_practitioner_ref
Revises: 0001_create_audit_log
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_fhir_practitioner_ref"
down_revision: str | None = "0001_create_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the EHR-asserted actor column and its lookup index."""
    op.add_column(
        "audit_log",
        sa.Column("fhir_practitioner_ref", sa.String(length=512), nullable=True),
    )
    # Indexed for the same reason actor_id is: "what did this provider access"
    # is a question an auditor asks, and for launch-time EHR reads this column
    # is the only place the answer lives.
    op.create_index(
        "idx_audit_log_fhir_practitioner",
        "audit_log",
        ["fhir_practitioner_ref"],
    )


def downgrade() -> None:
    """Drop the column.

    Destructive: it discards the only recorded identity for every launch-time
    EHR read, and nothing can reconstruct those values afterwards. Intended for
    local development only, like the rest of this package's downgrades.
    """
    op.drop_index("idx_audit_log_fhir_practitioner", table_name="audit_log")
    op.drop_column("audit_log", "fhir_practitioner_ref")

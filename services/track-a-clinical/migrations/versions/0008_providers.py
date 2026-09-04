r"""Create the ``providers`` registry.

``encounters.provider_id`` is a UUID and the identity an EHR asserts is a FHIR
``Practitioner`` reference, whose id is ``[A-Za-z0-9\-\.]{1,64}`` and is
routinely not a UUID at all — HAPI answers ``"1"``. TASK-051c verifies that
identity and stores the reference on the launch record; nothing could turn it
into a value the core schema keys on. This table is that mapping.

``fhir_practitioner_ref`` is ``VARCHAR(512)`` and holds the reference as the
claim gave it, normally an absolute URL. Not the bare id: a ``Practitioner`` id
is unique only within one EHR, so storing ``Practitioner/1`` would merge two
people on two servers into one provider. Same column width, and the same
reasoning, as ``audit_log.fhir_practitioner_ref``.

The ``UNIQUE`` constraint is the invariant the resolver depends on rather than a
tidiness measure: resolution is a get-or-create, two launches by one practitioner
can race, and two rows would split one clinician's encounters in two with nothing
erroring.

**No foreign key from ``encounters.provider_id`` to this table.**
``POST /sessions/start`` still takes ``provider_id`` as an unauthenticated body
field in v1, so a foreign key would turn a documented-weak field into a hard
constraint and reject every existing row. It becomes free — and should be added —
when provider authentication lands in Phase 5. See CLAUDE.md, "Provider identity
— the registry that resolves an EHR practitioner".

Revision ID: 0008_providers
Revises: 0007_prior_auth_payer_outcome
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_providers"
down_revision: str | None = "0007_prior_auth_payer_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the providers table."""
    # No column comments, for the reason given in 0002: nothing else in this
    # schema carries one and the drift guard compares them.
    op.create_table(
        "providers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("fhir_practitioner_ref", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fhir_practitioner_ref", name="uq_providers_fhir_practitioner_ref"),
    )


def downgrade() -> None:
    """Drop the providers table."""
    op.drop_table("providers")

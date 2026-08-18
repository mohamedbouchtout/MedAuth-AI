"""Create the core clinical schema.

Five tables: encounters and the three child tables that hang off it, plus the
standalone insurance_policies index of ingested payer documents. The schema is
authoritative in TASKS.md TASK-005; the mapped classes in
``track_a_clinical.models`` mirror it, and the integration test asserts the two
have not drifted.

audit_log is not created here — packages/hipaa-logger owns it and applies first.

Revision ID: 0001_create_core_schema
Revises:
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_create_core_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the five core tables and their indexes."""
    op.create_table(
        "encounters",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ehr_encounter_id", sa.String(length=100), nullable=True),
        sa.Column("patient_fhir_id", sa.String(length=100), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("insurance_payer", sa.String(length=200), nullable=True),
        sa.Column("insurance_plan_type", sa.String(length=100), nullable=True),
        sa.Column("insurance_member_id", sa.String(length=100), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("session_id", name="uq_encounters_session_id"),
    )
    op.create_index("idx_encounters_session", "encounters", ["session_id"])
    op.create_index("idx_encounters_provider", "encounters", ["provider_id"])

    op.create_table(
        "clinical_notes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "encounter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("encounters.id"),
            nullable=False,
        ),
        sa.Column("soap_subjective", sa.Text(), nullable=True),
        sa.Column("soap_objective", sa.Text(), nullable=True),
        sa.Column("soap_assessment", sa.Text(), nullable=True),
        sa.Column("soap_plan", sa.Text(), nullable=True),
        sa.Column("icd10_codes", postgresql.JSONB(), nullable=True),
        sa.Column("cpt_codes", postgresql.JSONB(), nullable=True),
        sa.Column("ehr_document_ref_id", sa.String(length=100), nullable=True),
        sa.Column(
            "generated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "reviewed_by_provider",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "provider_edited",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("idx_clinical_notes_encounter", "clinical_notes", ["encounter_id"])

    op.create_table(
        "clinical_nudges",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "encounter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("encounters.id"),
            nullable=False,
        ),
        sa.Column("procedure_name", sa.String(length=200), nullable=True),
        sa.Column("cpt_code", sa.String(length=20), nullable=True),
        sa.Column("nudge_message", sa.Text(), nullable=True),
        sa.Column("missing_criteria", postgresql.JSONB(), nullable=True),
        sa.Column("denial_risk", sa.String(length=20), nullable=True),
        sa.Column("payer_policy_source", sa.String(length=500), nullable=True),
        sa.Column(
            "fired_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "acknowledged",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resulted_in_documentation", sa.Boolean(), nullable=True),
    )
    op.create_index("idx_clinical_nudges_encounter", "clinical_nudges", ["encounter_id"])

    op.create_table(
        "prior_auth_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "encounter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("encounters.id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=50),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("payer_name", sa.String(length=200), nullable=True),
        sa.Column("procedures", postgresql.JSONB(), nullable=True),
        sa.Column("diagnoses", postgresql.JSONB(), nullable=True),
        sa.Column("clinical_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("submission_method", sa.String(length=50), nullable=True),
        sa.Column("payer_reference_number", sa.String(length=200), nullable=True),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("denial_reason", sa.Text(), nullable=True),
    )
    op.create_index("idx_prior_auth_encounter", "prior_auth_requests", ["encounter_id"])
    op.create_index("idx_prior_auth_status", "prior_auth_requests", ["status"])

    op.create_table(
        "insurance_policies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("payer", sa.String(length=200), nullable=False),
        sa.Column("plan_type", sa.String(length=100), nullable=True),
        sa.Column("state", sa.CHAR(length=2), nullable=True),
        sa.Column("policy_id", sa.String(length=200), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "last_ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column(
            "qdrant_collection",
            sa.String(length=100),
            nullable=False,
            server_default=sa.text("'insurance_policies'"),
        ),
        sa.UniqueConstraint("policy_id", name="uq_insurance_policies_policy_id"),
    )
    op.create_index(
        "idx_insurance_policies_payer_state",
        "insurance_policies",
        ["payer", "state"],
    )


def downgrade() -> None:
    """Drop the five core tables, children before parent.

    Destructive: this discards clinical records. Intended for local development
    and for the migration test that proves the history is reversible — never run
    against an environment holding real encounters.
    """
    op.drop_index("idx_insurance_policies_payer_state", table_name="insurance_policies")
    op.drop_table("insurance_policies")

    op.drop_index("idx_prior_auth_status", table_name="prior_auth_requests")
    op.drop_index("idx_prior_auth_encounter", table_name="prior_auth_requests")
    op.drop_table("prior_auth_requests")

    op.drop_index("idx_clinical_nudges_encounter", table_name="clinical_nudges")
    op.drop_table("clinical_nudges")

    op.drop_index("idx_clinical_notes_encounter", table_name="clinical_notes")
    op.drop_table("clinical_notes")

    op.drop_index("idx_encounters_provider", table_name="encounters")
    op.drop_index("idx_encounters_session", table_name="encounters")
    op.drop_table("encounters")

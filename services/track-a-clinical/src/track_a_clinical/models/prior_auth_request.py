"""The ``prior_auth_requests`` table — one row per authorization submission.

Migrated here, written by the prior-auth service (TASK-060 assembles the bundle,
TASK-061 routes the submission). ``clinical_evidence`` holds transcript excerpts,
so this table carries PHI and every read of it must be audit-logged.

Like ``clinical_nudges``, there is no ``deleted_at``: a submitted authorization is
a record of what was sent to a payer and cannot be retracted locally.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from track_a_clinical.models.base import Base, JsonObject, timestamp_column, uuid_primary_key

if TYPE_CHECKING:
    from track_a_clinical.models.encounter import Encounter

#: Lifecycle used by TASK-060/061. Free text rather than an enum so a payer-specific
#: state can be added without a migration.
PRIOR_AUTH_STATUS_PENDING = "pending"
PRIOR_AUTH_STATUS_SUBMITTED = "submitted"
PRIOR_AUTH_STATUS_APPROVED = "approved"
PRIOR_AUTH_STATUS_DENIED = "denied"


class PriorAuthRequest(Base):
    """A prior-authorization bundle and whatever the payer did with it."""

    __tablename__ = "prior_auth_requests"
    __table_args__ = (
        sa.Index("idx_prior_auth_encounter", "encounter_id"),
        sa.Index("idx_prior_auth_status", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_primary_key()
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("encounters.id"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        sa.String(50),
        nullable=False,
        server_default=sa.text(f"'{PRIOR_AUTH_STATUS_PENDING}'"),
    )
    payer_name: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)

    #: Procedure objects taken from the encounter's nudges (procedure_name, cpt_code).
    procedures: Mapped[list[JsonObject] | None] = mapped_column(
        postgresql.JSONB(),
        nullable=True,
    )
    #: Diagnosis objects taken from the clinical note's ICD-10 extraction.
    diagnoses: Mapped[list[JsonObject] | None] = mapped_column(
        postgresql.JSONB(),
        nullable=True,
    )
    #: Transcript excerpts tied to the flagged procedures — never the whole
    #: transcript. Narrowing this is a HIPAA minimum-necessary decision, not a
    #: payload-size one.
    clinical_evidence: Mapped[list[JsonObject] | None] = mapped_column(
        postgresql.JSONB(),
        nullable=True,
    )

    #: How it went out — FHIR PAS, CoverMyMeds, or fax (TASK-061 routes this).
    submission_method: Mapped[str | None] = mapped_column(sa.String(50), nullable=True)
    payer_reference_number: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)

    submitted_at: Mapped[datetime.datetime | None] = timestamp_column(nullable=True)
    decided_at: Mapped[datetime.datetime | None] = timestamp_column(nullable=True)
    denial_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    encounter: Mapped[Encounter] = relationship(
        "Encounter",
        back_populates="prior_auth_requests",
    )

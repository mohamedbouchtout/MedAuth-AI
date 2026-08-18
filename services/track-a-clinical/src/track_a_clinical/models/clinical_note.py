"""The ``clinical_notes`` table — one generated SOAP note per encounter.

Written by track-a-clinical (TASK-030) after the ``session:ended`` signal, then
read by prior-auth (TASK-060) when assembling a bundle and by apps/web (TASK-090)
for provider review.

Every column here is PHI. Reads must be recorded with ``hipaa_logger.audit_log``.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from track_a_clinical.models.base import (
    Base,
    JsonObject,
    soft_delete_column,
    timestamp_column,
    uuid_primary_key,
)

if TYPE_CHECKING:
    from track_a_clinical.models.encounter import Encounter


class ClinicalNote(Base):
    """A SOAP note plus the codes extracted alongside it."""

    __tablename__ = "clinical_notes"
    __table_args__ = (sa.Index("idx_clinical_notes_encounter", "encounter_id"),)

    id: Mapped[uuid.UUID] = uuid_primary_key()
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("encounters.id"),
        nullable=False,
    )

    soap_subjective: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    soap_objective: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    soap_assessment: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    soap_plan: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    #: Objects rather than bare code strings, because TASK-031 attaches a
    #: Comprehend Medical confidence score to each code the LLM proposed.
    icd10_codes: Mapped[list[JsonObject] | None] = mapped_column(
        postgresql.JSONB(),
        nullable=True,
    )
    #: Anticipated procedure codes from the same extraction pass — the input to
    #: the policy lookup that decides whether a nudge fires.
    cpt_codes: Mapped[list[JsonObject] | None] = mapped_column(
        postgresql.JSONB(),
        nullable=True,
    )

    #: The DocumentReference id returned when the note was written back to the EHR.
    #: Null until write-back succeeds, which is how an unsynced note is found.
    ehr_document_ref_id: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)

    generated_at: Mapped[datetime.datetime] = timestamp_column(nullable=False, default_now=True)
    reviewed_by_provider: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("FALSE"),
    )
    provider_edited: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("FALSE"),
    )

    deleted_at: Mapped[datetime.datetime | None] = soft_delete_column()

    encounter: Mapped[Encounter] = relationship("Encounter", back_populates="notes")

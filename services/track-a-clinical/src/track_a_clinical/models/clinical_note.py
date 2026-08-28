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
    __table_args__ = (
        # One note per encounter, enforced rather than assumed. TASK-060 reads
        # this table expecting zero or one row, and the writer is a Redis
        # consumer: a redelivered ``session:ended`` signal, a reconnect, or a
        # retry of a failed generation would otherwise each spend a Sonnet call
        # and leave a second row, with nothing raising anywhere along the way.
        # TASK-030's insert names this constraint as its ON CONFLICT target.
        #
        # A plain UNIQUE rather than an index partial on ``deleted_at IS NULL``:
        # nothing in the system soft-deletes a note today — TASK-032 edits them
        # in place — so the narrower form would buy a case that cannot arise.
        # Whatever first deletes a note has to revisit this.
        sa.UniqueConstraint("encounter_id", name="uq_clinical_notes_encounter"),
        # Redundant against the constraint's own implicit index, and kept
        # because TASK-005 specifies it by name and a migration test asserts it.
        sa.Index("idx_clinical_notes_encounter", "encounter_id"),
    )

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
    #: Comprehend Medical confidence score to each code the LLM proposed. The
    #: shape is :class:`~track_a_clinical.models.extracted_code.ExtractedCode`;
    #: write through it rather than assembling dictionaries here.
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

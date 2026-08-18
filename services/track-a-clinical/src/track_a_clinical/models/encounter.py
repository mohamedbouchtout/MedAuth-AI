"""The ``encounters`` table — one row per clinical visit.

This is the root of the schema: notes, nudges and prior-auth requests all hang
off an encounter. ``session_id`` is the identifier that travels through Redis
channels and session JWTs (TASK-006), which is why it is unique rather than just
indexed — two encounters sharing a session would make every
``transcription:{session_id}`` subscriber ambiguous.

Insurance and patient columns hold PHI. Any read that returns them to a caller
must be recorded with ``hipaa_logger.audit_log``.
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
    soft_delete_column,
    timestamp_column,
    uuid_primary_key,
)

if TYPE_CHECKING:
    from track_a_clinical.models.clinical_note import ClinicalNote
    from track_a_clinical.models.clinical_nudge import ClinicalNudge
    from track_a_clinical.models.prior_auth_request import PriorAuthRequest

#: Set at creation by ``POST /sessions/start`` and moved to ``completed`` by
#: ``POST /sessions/{session_id}/end`` (TASK-006).
ENCOUNTER_STATUS_ACTIVE = "active"
ENCOUNTER_STATUS_COMPLETED = "completed"


class Encounter(Base):
    """A single physician-patient encounter."""

    __tablename__ = "encounters"
    __table_args__ = (
        sa.UniqueConstraint("session_id", name="uq_encounters_session_id"),
        sa.Index("idx_encounters_session", "session_id"),
        sa.Index("idx_encounters_provider", "provider_id"),
    )

    id: Mapped[uuid.UUID] = uuid_primary_key()

    #: Correlates the encounter across Redis channels, session JWTs and WebSockets.
    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    #: The EHR's own Encounter resource id, when the visit was launched from one.
    ehr_encounter_id: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    patient_fhir_id: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        sa.String(20),
        nullable=False,
        server_default=sa.text(f"'{ENCOUNTER_STATUS_ACTIVE}'"),
    )
    started_at: Mapped[datetime.datetime] = timestamp_column(nullable=False, default_now=True)
    ended_at: Mapped[datetime.datetime | None] = timestamp_column(nullable=True)

    #: Copied from the Coverage resource at launch. Denormalized on purpose: the
    #: RAG cache key is `rag:{payer}:{plan_type}:{state}:{cpt_code}`, and a policy
    #: query during the encounter cannot wait on a round trip to the EHR.
    insurance_payer: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    insurance_plan_type: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    insurance_member_id: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)

    deleted_at: Mapped[datetime.datetime | None] = soft_delete_column()

    notes: Mapped[list[ClinicalNote]] = relationship(
        "ClinicalNote",
        back_populates="encounter",
    )
    nudges: Mapped[list[ClinicalNudge]] = relationship(
        "ClinicalNudge",
        back_populates="encounter",
    )
    prior_auth_requests: Mapped[list[PriorAuthRequest]] = relationship(
        "PriorAuthRequest",
        back_populates="encounter",
    )

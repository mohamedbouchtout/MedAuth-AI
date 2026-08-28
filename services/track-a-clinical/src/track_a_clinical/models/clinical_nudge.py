"""The ``clinical_nudges`` table — one row per alert fired during an encounter.

Migrated here because track-a-clinical owns the schema, but written by
track-b-rag (TASK-040) when a policy query comes back with unmet criteria. That
split is intentional and documented under "Migration Ownership vs. Table Write
Access" in CLAUDE.md; importing this class is how the writing service stays in
step with the migration history.

There is no ``deleted_at`` column. A nudge is a record of what the system told a
provider at a point in time, so rows are never retired — the same reasoning that
keeps ``audit_log`` append-only.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from track_a_clinical.models.base import Base, timestamp_column, uuid_primary_key

if TYPE_CHECKING:
    from track_a_clinical.models.encounter import Encounter

#: Values track-b-rag assigns in TASK-040. Stored as free text rather than a
#: PostgreSQL enum so adding a level later is a code change, not a migration.
DENIAL_RISK_LOW = "low"
DENIAL_RISK_MEDIUM = "medium"
DENIAL_RISK_HIGH = "high"


class ClinicalNudge(Base):
    """A prior-authorization alert raised mid-encounter."""

    __tablename__ = "clinical_nudges"
    __table_args__ = (
        sa.Index("idx_clinical_nudges_encounter", "encounter_id"),
        # One nudge per procedure per encounter (migration 0005). Partial,
        # because TASK-044's keyword-only nudges carry no code and NULLs do not
        # collide — see the migration for why that half waits for its own task.
        sa.Index(
            "uq_clinical_nudges_encounter_cpt",
            "encounter_id",
            "cpt_code",
            unique=True,
            postgresql_where=sa.text("cpt_code IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_primary_key()
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("encounters.id"),
        nullable=False,
    )

    procedure_name: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    cpt_code: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    nudge_message: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    #: The payer criteria the encounter has not yet satisfied, as plain strings —
    #: the same ``missing_criteria: list[str]`` the policy query returns (TASK-012).
    missing_criteria: Mapped[list[str] | None] = mapped_column(
        postgresql.JSONB(),
        nullable=True,
    )
    denial_risk: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    #: Where the criteria came from — a payer policy URL or document identifier.
    #: Not PHI, and the one field that makes a nudge auditable after the fact.
    payer_policy_source: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)

    fired_at: Mapped[datetime.datetime] = timestamp_column(nullable=False, default_now=True)
    acknowledged: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("FALSE"),
    )
    acknowledged_at: Mapped[datetime.datetime | None] = timestamp_column(nullable=True)
    #: Whether the nudge actually changed the note. Nullable on purpose: unknown
    #: until the note is generated, and "not yet evaluated" is not the same as
    #: "made no difference".
    resulted_in_documentation: Mapped[bool | None] = mapped_column(sa.Boolean(), nullable=True)

    encounter: Mapped[Encounter] = relationship("Encounter", back_populates="nudges")

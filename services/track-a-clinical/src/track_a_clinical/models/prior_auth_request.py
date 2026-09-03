"""The ``prior_auth_requests`` table — one row per authorization submission.

Migrated here and written by two services: prior-auth assembles the bundle
(TASK-060) and routes the submission (TASK-061), and fhir-integration records
what a FHIR PAS submission came back with (TASK-054). ``clinical_evidence``
holds transcript excerpts, so this table carries PHI and every read of it must
be audit-logged.

Like ``clinical_nudges``, there is no ``deleted_at``: a submitted authorization is
a record of what was sent to a payer and cannot be retracted locally.
"""

from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from track_a_clinical.models.base import Base, JsonObject, timestamp_column, uuid_primary_key

if TYPE_CHECKING:
    from track_a_clinical.models.encounter import Encounter

#: Lifecycle used by TASK-060/061. Free text rather than an enum so a payer-specific
#: state can be added without a migration.
PRIOR_AUTH_STATUS_PENDING = "pending"
PRIOR_AUTH_STATUS_SUBMITTED = "submitted"
PRIOR_AUTH_STATUS_APPROVED = "approved"
PRIOR_AUTH_STATUS_DENIED = "denied"


class SubmissionMethod(StrEnum):
    """How a prior authorization left this system.

    Closed, unlike the status constants above, and the difference is not
    inconsistency. A status is a payer's word for where a request has got to, and
    the comment above says so — a payer-specific state has to be addable without
    a migration. A submission method is *our* word for which code path sent it,
    so the set is knowable in advance and nothing outside this repository can
    add to it.

    It is closed **now** because TASK-054 creates the second value. It was a bare
    ``str`` while only FHIR PAS existed, which was TASK-050 deliberately
    declining to fix a vocabulary it could not yet see. This is the fourth
    instance of the pattern ``payer_vocab``'s canonical slugs,
    ``hipaa_logger.AuditAction`` and ``adapters.factory.EHRType`` already
    established, each converted at the moment a second real value appeared, and
    for the same reason each time: the value is compared by string equality and
    round-trips through a ``VARCHAR(50)`` that constrains nothing on its own, so
    the type is the only thing standing between one vocabulary and two spellings
    of it.

    ``StrEnum`` rather than ``Enum`` for the reason ``AuditAction`` and
    ``EHRType`` are: a member compares equal to its own text, so what goes into
    the column and comes back out is an ordinary string and no serialisation step
    has to know the type exists.
    """

    #: Da Vinci PAS — ``POST [base]/Claim/$submit`` against the payer's own FHIR
    #: endpoint. The base adapter's path (TASK-054).
    FHIR_PAS = "fhir-pas"

    #: CoverMyMeds, for a payer or EHR with no FHIR PAS support. Athenahealth is
    #: the first, which is why ``AthenaAdapter`` overrides the submission
    #: (TASK-054).
    COVERMYMEDS = "covermymeds"

    #: Fax, the floor every payer still accepts. Named in this column's own
    #: comment since TASK-005 and routed by TASK-061; a member for unbuilt work
    #: is expected here for the same reason ``AuditAction`` carries one, and an
    #: unused member is inert in a way a documented-but-absent value is not.
    FAX = "fax"


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

    #: How it went out. One of :class:`SubmissionMethod`, or NULL before the
    #: request has been submitted at all. Stored as text rather than a database
    #: enum so that adding a method needs no migration; what makes that safe is
    #: the validator below rather than the column type.
    submission_method: Mapped[str | None] = mapped_column(sa.String(50), nullable=True)
    payer_reference_number: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)

    submitted_at: Mapped[datetime.datetime | None] = timestamp_column(nullable=True)
    decided_at: Mapped[datetime.datetime | None] = timestamp_column(nullable=True)
    denial_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    encounter: Mapped[Encounter] = relationship(
        "Encounter",
        back_populates="prior_auth_requests",
    )

    @validates("submission_method")
    def _validate_submission_method(self, _key: str, value: str | None) -> str | None:
        """Refuse a submission method outside :class:`SubmissionMethod`.

        The enum makes a wrong value a type error at a call site mypy reaches.
        This is the backstop for the callers it does not: two services write this
        column — ``fhir-integration`` records what it submitted (TASK-054) and
        ``prior-auth`` routes and records its own (TASK-061) — and a value that
        arrived over HTTP has already left the type system by the time it gets
        here. Both go through this mapped class, per CLAUDE.md's rule that the
        shared tables have exactly one set of models, so this is the single point
        every write passes.

        Raising beats coercing. A method we cannot name is not a submission we
        can honestly say happened, and silently storing ``"FHIR_PAS"`` beside
        ``"fhir-pas"`` is the two-spellings failure the enum exists to prevent.
        """
        if value is None:
            return None
        try:
            return SubmissionMethod(value).value
        except ValueError:
            permitted = ", ".join(sorted(method.value for method in SubmissionMethod))
            raise ValueError(
                f"submission_method must be one of {permitted}; got {value!r}"
            ) from None

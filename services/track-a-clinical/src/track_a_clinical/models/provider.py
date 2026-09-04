r"""The ``providers`` table — one row per practitioner an EHR has asserted.

``encounters.provider_id`` is a UUID, and the identity an EHR gives us is not.
SMART on FHIR names the authorizing user through the ``fhirUser`` claim, which
resolves to a ``Practitioner`` reference, and a FHIR ``id`` is
``[A-Za-z0-9\-\.]{1,64}`` — HAPI answers ``"1"``. This table is the mapping
between the two, so a launch can produce a provider identifier the rest of the
schema can actually key on.

**Why a table rather than a derivation.** Deriving a UUIDv5 from the reference
would need no row and no migration, and it is one-way: nothing would record which
practitioner a derived UUID came from, so every audit row the Redis consumers
write would carry an actor no query can resolve to a person — and carry it
opaquely, looking exactly like a provider this system knows. A row is what makes
``provider_id`` answerable. See CLAUDE.md, "Provider identity — the registry that
resolves an EHR practitioner", for the alternatives and what this costs.

**A practitioner reference is not PHI.** It identifies the provider, not a
patient, so writing this table is an operational event and is logged rather than
audited — Known Constraints #6 is an if-and-only-if in both directions.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from track_a_clinical.models.base import (
    Base,
    soft_delete_column,
    timestamp_column,
    uuid_primary_key,
)


class Provider(Base):
    """A practitioner an EHR asserted and this system verified."""

    __tablename__ = "providers"
    __table_args__ = (
        # The invariant the get-or-create resolution depends on: one
        # practitioner is one provider. Enforced here rather than left to the
        # resolver, because two launches by the same person can race, and two
        # rows would silently split one clinician's encounters in two.
        sa.UniqueConstraint("fhir_practitioner_ref", name="uq_providers_fhir_practitioner_ref"),
    )

    id: Mapped[uuid.UUID] = uuid_primary_key()

    #: The ``Practitioner`` reference as the verified ``fhirUser`` claim gave it —
    #: normally an absolute URL such as
    #: ``https://ehr.example.com/fhir/Practitioner/abc-123``.
    #:
    #: **Never the bare id.** A ``Practitioner`` id is unique only within one
    #: EHR, so ``Practitioner/1`` on two servers is two different people and
    #: storing the id alone would merge them into one provider. The same
    #: reasoning, and the same 512 characters, as
    #: ``audit_log.fhir_practitioner_ref``.
    fhir_practitioner_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)

    created_at: Mapped[datetime.datetime] = timestamp_column(nullable=False, default_now=True)

    deleted_at: Mapped[datetime.datetime | None] = soft_delete_column()

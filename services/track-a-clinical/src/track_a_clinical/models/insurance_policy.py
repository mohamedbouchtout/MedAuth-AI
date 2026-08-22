"""The ``insurance_policies`` table — the index of what has been ingested.

Written by the policy scraper (TASK-011/013) and read by track-b-rag. It holds no
PHI: insurance policy documents are public payer publications. It is the relational
counterpart to the Qdrant collection — Qdrant stores the chunk vectors, this table
records which policy document each collection was built from and when.

The table stands alone; it has no foreign key to ``encounters``. It lives in this
migration set only because track-a-clinical owns migration authorship for the
shared database.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from track_a_clinical.models.base import Base, timestamp_column, uuid_primary_key

#: Default Qdrant collection, matching the one TASK-010 creates.
DEFAULT_QDRANT_COLLECTION = "insurance_policies"


class InsurancePolicy(Base):
    """One ingested payer policy document."""

    __tablename__ = "insurance_policies"
    __table_args__ = (
        sa.UniqueConstraint("policy_id", name="uq_insurance_policies_policy_id"),
        sa.Index("idx_insurance_policies_payer_state", "payer", "state"),
    )

    id: Mapped[uuid.UUID] = uuid_primary_key()

    payer: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    plan_type: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    #: Two-letter state code for a policy that applies in exactly one state, or
    #: null. Null alone means national; see ``jurisdiction_states`` for the
    #: middle case, a policy issued per Medicare contractor jurisdiction.
    state: Mapped[str | None] = mapped_column(sa.CHAR(2), nullable=True)

    #: The USPS state codes a contractor-jurisdiction policy applies in — a
    #: median of twelve for a Medicare LCD, up to forty-eight. One row per
    #: document with a list here, rather than one row per state with a composite
    #: policy_id, which would duplicate the same policy text a dozen times over
    #: in Qdrant. Null for single-state and national documents.
    jurisdiction_states: Mapped[list[str] | None] = mapped_column(
        postgresql.ARRAY(sa.Text()), nullable=True
    )

    #: The payer's own identifier for the document. Unique, so a re-scrape updates
    #: the existing row instead of accumulating duplicates.
    policy_id: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    source_url: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    #: SHA-256 hex digest of the source document (TASK-011). Re-embedding is
    #: skipped when the digest is unchanged, which is what keeps the nightly
    #: scrape from re-indexing every policy every night.
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    last_ingested_at: Mapped[datetime.datetime] = timestamp_column(
        nullable=False,
        default_now=True,
    )
    #: The payer's stated effective date — a plain date, not a timestamp, because
    #: that is how policy documents express it.
    effective_date: Mapped[datetime.date | None] = mapped_column(sa.Date(), nullable=True)

    qdrant_collection: Mapped[str] = mapped_column(
        sa.String(100),
        nullable=False,
        server_default=sa.text(f"'{DEFAULT_QDRANT_COLLECTION}'"),
    )

"""Seed five test encounters for local development.

    DATABASE_URL=postgresql+asyncpg://... uv run python scripts/seed-test-encounters.py

Run ``scripts/init-db.sh`` first — this script inserts rows, it does not create
tables. It is idempotent: every row gets a UUID derived from a fixed namespace,
so re-running updates nothing and inserts nothing new.

PLACEHOLDER PATIENT IDS
-----------------------
``patient_fhir_id`` is seeded with ``synthea-placeholder-N`` rather than real
Synthea patient identifiers, because the local HAPI FHIR server has no patients
loaded until TASK-052 runs ``scripts/seed-synthea.sh``. Once that lands, replace
the ``patient_fhir_id`` values in ``SEED_ENCOUNTERS`` with real Patient resource ids
from HAPI so that fhir-integration can resolve them. Nothing else in this file
needs to change.

No PHI is involved either way: Synthea patients are synthetic by construction.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from track_a_clinical.db import DatabaseConfigurationError, database_url
from track_a_clinical.models import ENCOUNTER_STATUS_ACTIVE, Encounter

logger: Final = logging.getLogger("seed-test-encounters")

#: Fixed namespace so every run derives the same ids and the seed stays idempotent.
#: Any UUID works here as long as it never changes.
SEED_NAMESPACE: Final = uuid.UUID("6f1c0f5e-6a1a-4f9d-9a1b-2c9a0d5e7b31")

#: One synthetic provider owns all five encounters — enough to exercise the
#: provider_id index without inventing an org chart.
TEST_PROVIDER_ID: Final = uuid.uuid5(SEED_NAMESPACE, "provider/dr-test")
TEST_ORGANIZATION_ID: Final = uuid.uuid5(SEED_NAMESPACE, "organization/test-clinic")


@dataclass(frozen=True)
class SeedEncounter:
    """One row to insert, keyed by a stable slug."""

    slug: str
    patient_fhir_id: str
    insurance_payer: str
    insurance_plan_type: str

    @property
    def session_id(self) -> uuid.UUID:
        """Derive the session id from the slug so re-runs collide instead of duplicating."""
        return uuid.uuid5(SEED_NAMESPACE, f"session/{self.slug}")

    @property
    def ehr_encounter_id(self) -> str:
        """A stand-in for the EHR's own Encounter resource id."""
        return f"ehr-encounter-{self.slug}"


# Payers and plan types are varied on purpose. Both feed the RAG cache key
# `rag:{payer}:{plan_type}:{state}:{cpt_code}`, so a seed set that shared one
# payer would hide cache-collision bugs in TASK-012.
SEED_ENCOUNTERS: Final[tuple[SeedEncounter, ...]] = (
    SeedEncounter("ortho-knee", "synthea-placeholder-1", "Aetna", "PPO"),
    SeedEncounter("ortho-shoulder", "synthea-placeholder-2", "UnitedHealthcare", "HMO"),
    SeedEncounter("derm-lesion", "synthea-placeholder-3", "Cigna", "PPO"),
    SeedEncounter("derm-mohs", "synthea-placeholder-4", "Blue Cross Blue Shield", "EPO"),
    SeedEncounter("ortho-spine", "synthea-placeholder-5", "Humana", "Medicare Advantage"),
)


async def seed(database_url_value: str) -> int:
    """Insert any missing seed encounters. Returns the number of rows inserted."""
    engine = create_async_engine(database_url_value)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    inserted = 0
    try:
        async with session_factory() as session:
            for seed_row in SEED_ENCOUNTERS:
                existing = await session.scalar(
                    select(Encounter.id).where(Encounter.session_id == seed_row.session_id)
                )
                if existing is not None:
                    logger.info("%s already seeded — skipping", seed_row.slug)
                    continue
                session.add(
                    Encounter(
                        session_id=seed_row.session_id,
                        ehr_encounter_id=seed_row.ehr_encounter_id,
                        patient_fhir_id=seed_row.patient_fhir_id,
                        provider_id=TEST_PROVIDER_ID,
                        organization_id=TEST_ORGANIZATION_ID,
                        status=ENCOUNTER_STATUS_ACTIVE,
                        insurance_payer=seed_row.insurance_payer,
                        insurance_plan_type=seed_row.insurance_plan_type,
                        insurance_member_id=f"member-{seed_row.slug}",
                    )
                )
                inserted += 1
                logger.info("seeded %s", seed_row.slug)
            await session.commit()
    finally:
        await engine.dispose()
    return inserted


def main() -> int:
    """Entry point. Returns a process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        url = database_url()
    except DatabaseConfigurationError as exc:
        logger.error("%s", exc)
        return 1

    inserted = asyncio.run(seed(url))
    logger.info("done — %d inserted, %d already present", inserted, len(SEED_ENCOUNTERS) - inserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())

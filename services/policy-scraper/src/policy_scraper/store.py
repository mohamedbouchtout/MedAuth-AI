"""Reading what has already been ingested, to avoid uploading it again.

This is a bandwidth optimisation and nothing more. ``/policies/ingest`` computes
the digest from the bytes it receives and decides created/unchanged/updated for
itself; that decision is the authoritative one. Skipping here just saves sending
a document CMS has not touched since last night — with a few dozen documents a
run, that is most of them.

Because it is only an optimisation, losing a race costs one redundant upload and
never a wrong answer. A stale read means we upload and ingest reports
``unchanged``; a missing read means the same. Nothing here needs a lock.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from track_a_clinical.models import InsurancePolicy

logger = logging.getLogger(__name__)


def session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    """Return a session factory for the shared database.

    Accepts the one ``DATABASE_URL`` every service uses. The SQLAlchemy dialect
    form is what CI and `.env.example` set, and a bare ``postgresql://`` is
    accepted too so a caller that copied a psql URL is not told it is wrong.
    """
    url = database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return async_sessionmaker(create_async_engine(url), expire_on_commit=False)


async def known_content_hashes(session: AsyncSession, policy_ids: set[str]) -> dict[str, str]:
    """Return the stored digest for each policy id that has one.

    One query for the whole run rather than one per document: the scrape knows
    every id it is about to consider before it uploads any of them, and a few
    dozen round trips to save a few dozen uploads is a poor trade.
    """
    if not policy_ids:
        return {}

    rows = await session.execute(
        sa.select(InsurancePolicy.policy_id, InsurancePolicy.content_hash).where(
            InsurancePolicy.policy_id.in_(policy_ids)
        )
    )
    known = {policy_id: content_hash for policy_id, content_hash in rows}
    logger.info("%s of %s policies are already recorded", len(known), len(policy_ids))
    return known

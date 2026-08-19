"""The policy ingestion pipeline: PDF in, indexed chunks and one row out.

The three-way dedup TASK-011 specifies, keyed on ``(policy_id, content_hash)``:

* no row for this ``policy_id`` — index it, insert the row, report ``created``
* a row exists and the digest matches — do nothing, report ``unchanged``
* a row exists and the digest differs — re-index it, update the row, report
  ``updated``

**Qdrant is written before Postgres, and that ordering is load-bearing.** The
two stores share no transaction, so the order decides which way a partial
failure fails. Writing vectors first means a crash in between leaves the stored
``content_hash`` stale: the next scrape sees a mismatch and re-ingests. Wasteful,
self-correcting, and visible. Reversed, the row would claim to be current while
its vectors were missing or half-replaced, and nothing would ever retry —
retrieval would quietly return less than it should, or return chunks of a
superseded policy. TASKS.md records this so it does not get "simplified" later.

Nothing here writes an audit row. Insurance policy documents are public payer
publications with no patient linkage, and Known Constraints #6 makes auditing
conditional on touching PHI precisely so the audit trail stays a trail of PHI
accesses. The INFO log below is the operational record instead.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Literal

import sqlalchemy as sa
from qdrant_client import QdrantClient
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from track_a_clinical.models import InsurancePolicy
from track_b_rag import embeddings, vector_store
from track_b_rag.chunking import chunk_text
from track_b_rag.pdf import content_digest, extract_text

logger = logging.getLogger(__name__)

IngestStatus = Literal["created", "updated", "unchanged"]


class EmptyDocumentError(ValueError):
    """Raised when a readable PDF yields no text to index.

    A scanned policy with no text layer parses cleanly and produces nothing.
    Accepting it would write a policy row whose ``content_hash`` says "current"
    with zero vectors behind it — the one state the dedup logic cannot recover
    from on its own, since every later ingest of the same bytes reports
    ``unchanged``.
    """


@dataclass(frozen=True)
class PolicyMetadata:
    """The non-file half of an ingest request."""

    policy_id: str
    payer: str
    plan_type: str | None = None
    state: str | None = None
    source_url: str | None = None
    effective_date: datetime.date | None = None


@dataclass(frozen=True)
class IngestResult:
    """What one ingest call did."""

    policy_id: str
    status: IngestStatus
    content_hash: str
    chunks_indexed: int
    collection: str


async def ingest_policy(
    *,
    session: AsyncSession,
    client: QdrantClient,
    collection: str,
    pdf_bytes: bytes,
    metadata: PolicyMetadata,
) -> IngestResult:
    """Index a policy document and record it, skipping work when nothing changed.

    Args:
        session: Database session; this function commits it on a real ingest.
        client: Qdrant client for the collection being written.
        collection: Collection name, normally ``QDRANT_COLLECTION``.
        pdf_bytes: The raw uploaded file.
        metadata: Payer and policy identifiers for the document.

    Returns:
        What happened, including the digest and the number of chunks written —
        zero for an ``unchanged`` result, which does no work at all.

    Raises:
        PdfParseError: The bytes are not a readable PDF.
        EmptyDocumentError: The PDF is readable but holds no extractable text.
    """
    digest = content_digest(pdf_bytes)
    existing = await session.scalar(
        sa.select(InsurancePolicy).where(InsurancePolicy.policy_id == metadata.policy_id)
    )

    if existing is not None and existing.content_hash == digest:
        logger.info(
            "Policy %r from %r is unchanged (%s); skipping re-ingestion",
            metadata.policy_id,
            metadata.payer,
            digest[:12],
        )
        return IngestResult(
            policy_id=metadata.policy_id,
            status="unchanged",
            content_hash=digest,
            chunks_indexed=0,
            collection=collection,
        )

    status: IngestStatus = "updated" if existing is not None else "created"
    chunks = await run_in_threadpool(_extract_chunks, pdf_bytes)
    vectors = await run_in_threadpool(embeddings.embed_documents, chunks)

    # Qdrant first — see the module docstring for why this order is not
    # arbitrary. The delete is scoped by policy_id rather than by the point IDs
    # this version produces, so a shorter revision cannot strand the old tail.
    if existing is not None:
        await run_in_threadpool(
            vector_store.delete_policy_points, client, collection, metadata.policy_id
        )
    points = vector_store.build_points(
        policy_id=metadata.policy_id,
        payer=metadata.payer,
        plan_type=metadata.plan_type,
        state=metadata.state,
        chunks=chunks,
        vectors=vectors,
    )
    await run_in_threadpool(vector_store.upsert_points, client, collection, points)

    await _record_policy(session, metadata=metadata, digest=digest, collection=collection)

    logger.info(
        "Ingested policy %r from %r: %s, %d chunks into %r (%s)",
        metadata.policy_id,
        metadata.payer,
        status,
        len(points),
        collection,
        digest[:12],
    )
    return IngestResult(
        policy_id=metadata.policy_id,
        status=status,
        content_hash=digest,
        chunks_indexed=len(points),
        collection=collection,
    )


def _extract_chunks(pdf_bytes: bytes) -> list[str]:
    """Parse and split, in one worker-thread hop rather than two."""
    chunks = chunk_text(extract_text(pdf_bytes))
    if not chunks:
        raise EmptyDocumentError(
            "The PDF parsed successfully but contains no extractable text. A scanned "
            "document needs OCR before it can be indexed."
        )
    return chunks


async def _record_policy(
    session: AsyncSession,
    *,
    metadata: PolicyMetadata,
    digest: str,
    collection: str,
) -> None:
    """Insert or refresh the ``insurance_policies`` row for this document.

    An ``ON CONFLICT DO UPDATE`` on the unique ``policy_id`` rather than a
    read-then-branch: two scrapers racing on the same new policy would otherwise
    both see no row and both insert, and the loser would surface as an integrity
    error on what is a perfectly ordinary retry. The created/updated distinction
    the caller reports comes from the read it already did — the race can make
    that label optimistic, never the write incorrect.
    """
    values = {
        "policy_id": metadata.policy_id,
        "payer": metadata.payer,
        "plan_type": metadata.plan_type,
        "state": metadata.state,
        "source_url": metadata.source_url,
        "effective_date": metadata.effective_date,
        "content_hash": digest,
        "qdrant_collection": collection,
        "last_ingested_at": sa.func.now(),
    }
    statement = pg_insert(InsurancePolicy).values(**values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[InsurancePolicy.policy_id],
            # policy_id is the conflict target; re-setting it would be a no-op.
            set_={key: value for key, value in values.items() if key != "policy_id"},
        )
    )
    await session.commit()

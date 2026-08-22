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

import dataclasses
import datetime
import logging
from dataclasses import dataclass
from typing import Literal

import sqlalchemy as sa
from qdrant_client import QdrantClient
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from payer_vocab import normalize_payer
from track_a_clinical.models import InsurancePolicy
from track_b_rag import embeddings, vector_store
from track_b_rag.chunking import chunk_text
from track_b_rag.documents import DEFAULT_CONTENT_TYPE, ContentType, content_digest, extract_text

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
    """The non-file half of an ingest request.

    ``payer`` is the payer's own spelling, kept as it arrived. Matching uses
    :attr:`payer_slug` instead — see :mod:`payer_vocab` for why the two are not
    the same field.
    """

    policy_id: str
    payer: str
    plan_type: str | None = None
    state: str | None = None
    source_url: str | None = None
    effective_date: datetime.date | None = None
    #: The states a contractor-jurisdiction policy applies in. Empty for a
    #: single-state policy (which uses ``state``) and for a national one.
    jurisdiction_states: list[str] = dataclasses.field(default_factory=list)
    #: What the caller says the upload is. Declared rather than sniffed — see
    #: :mod:`track_b_rag.documents`.
    content_type: ContentType = DEFAULT_CONTENT_TYPE

    @property
    def qdrant_state(self) -> str | list[str] | None:
        """What goes in the payload's ``state`` field.

        A list for a contractor jurisdiction, a string for a single state, and
        None for a national policy. The retrieval filter needs no branch for
        this: Qdrant's ``MatchValue`` matches any element of a list-valued
        payload field, and the ``IsNullCondition`` beside it still picks up the
        national documents.
        """
        return self.jurisdiction_states or self.state

    @property
    def payer_slug(self) -> str:
        """The canonical slug this document's chunks are indexed and matched under.

        The Qdrant payload carries this rather than ``payer``, because the
        retrieval filter compares it by exact equality against a slug derived
        from a FHIR ``Coverage`` display name. The `insurance_policies` row keeps
        the display spelling; nothing matches on that column.
        """
        return normalize_payer(self.payer)


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
    document_bytes: bytes,
    metadata: PolicyMetadata,
) -> IngestResult:
    """Index a policy document and record it, skipping work when nothing changed.

    Args:
        session: Database session; this function commits it on a real ingest.
        client: Qdrant client for the collection being written.
        collection: Collection name, normally ``QDRANT_COLLECTION``.
        document_bytes: The raw uploaded file, in the format
            ``metadata.content_type`` declares.
        metadata: Payer and policy identifiers for the document.

    Returns:
        What happened, including the digest and the number of chunks written —
        zero for an ``unchanged`` result, which does no work at all.

    Raises:
        DocumentParseError: The bytes are not readable in their declared format.
        EmptyDocumentError: The document is readable but holds no extractable
            text.
    """
    digest = content_digest(document_bytes)
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
    chunks = await run_in_threadpool(_extract_chunks, document_bytes, metadata.content_type)
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
        payer=metadata.payer_slug,
        plan_type=metadata.plan_type,
        state=metadata.qdrant_state,
        chunks=chunks,
        vectors=vectors,
    )
    await run_in_threadpool(vector_store.upsert_points, client, collection, points)

    await _record_policy(session, metadata=metadata, digest=digest, collection=collection)

    logger.info(
        "Ingested policy %r from %r (payer slug %r): %s, %d chunks into %r (%s)",
        metadata.policy_id,
        metadata.payer,
        metadata.payer_slug,
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


def _extract_chunks(document_bytes: bytes, content_type: ContentType) -> list[str]:
    """Parse and split, in one worker-thread hop rather than two.

    Chunking is the same for both formats — 800 characters with 150 of overlap —
    because both readers hand back plain text with block boundaries as blank
    lines. Policy prose is not shaped differently for having been published as
    HTML, and the constants are fixed for the reason `chunking` gives: changing
    either invalidates every chunk already indexed.
    """
    chunks = chunk_text(extract_text(document_bytes, content_type))
    if not chunks:
        raise EmptyDocumentError(
            "The document parsed successfully but contains no extractable text. A "
            "scanned PDF needs OCR before it can be indexed, and markup with no "
            "text in it has nothing to index."
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
        "jurisdiction_states": metadata.jurisdiction_states or None,
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

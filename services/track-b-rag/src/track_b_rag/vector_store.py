"""The Qdrant collection holding insurance policy chunks.

The one rule this module exists to enforce: **never** ``recreate_collection()``
outside a deliberate human-run reset. ``recreate_collection`` drops the
collection and builds an empty one, so calling it from startup code wipes every
indexed policy on each restart, rollout and pod reschedule — silently, because
an empty collection is a valid collection and retrieval just starts returning
nothing. CLAUDE.md fixes the get-or-create shape used below;
``tests/integration/test_vector_store.py`` is the regression test for it.

The client is a lazily-created singleton. Nothing connects at import time, so
the service starts and its health endpoint answers "error" while Qdrant is
still coming up, instead of the process failing to boot.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from functools import lru_cache
from typing import Final

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from track_b_rag.config import get_settings

logger = logging.getLogger(__name__)

#: Cosine, per TASK-010. bge embeddings are normalised, so cosine and dot
#: product rank identically — cosine is the one the ingestion and query tasks
#: were specified against.
DISTANCE = Distance.COSINE

#: Fixed namespace for deterministic point IDs. Never regenerate this value:
#: every point already in the collection was addressed through it, and a new
#: namespace would make re-ingestion write a second copy of every chunk instead
#: of overwriting the first.
POINT_NAMESPACE: Final = uuid.UUID("9d0f6a2b-1c84-4f5a-b3d7-0e6a5c1f8b42")

#: Payload keys carried by every policy chunk, fixed in TASK-011. ``text`` is
#: the chunk itself — TASK-012 reads it back to build the prompt, so it lives in
#: the payload rather than being reconstructed from the source PDF.
PAYLOAD_FIELDS: Final = ("policy_id", "payer", "plan_type", "state", "chunk_index", "text")

#: Indexed payload keys. ``policy_id`` serves the delete-by-filter path below;
#: ``payer`` and ``state`` serve TASK-012's retrieval filter. Qdrant will filter
#: on an unindexed field, but by scanning — these three are the ones on the hot
#: path, and the index is what keeps that path from degrading as the collection
#: grows.
INDEXED_PAYLOAD_FIELDS: Final = ("policy_id", "payer", "state")


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    """Return the process-wide Qdrant client, connected lazily on first call."""
    settings = get_settings()
    return QdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=settings.qdrant_api_key,
    )


def close_client() -> None:
    """Close the Qdrant client and forget it. Called on app shutdown."""
    if get_client.cache_info().currsize:
        get_client().close()
    get_client.cache_clear()


def ensure_collection(client: QdrantClient, name: str, vector_size: int) -> bool:
    """Create the collection if it is absent, and leave it alone if it is not.

    Returns True when this call created the collection, False when it already
    existed. Safe to call on every startup: an existing collection keeps every
    point it holds, which is the whole point of not using
    ``recreate_collection`` here.

    A concurrent creation by another replica is treated as success rather than
    an error — several pods starting together all run this, and losing that
    race means the collection exists, which is the outcome being asked for.
    """
    try:
        client.get_collection(name)
    except UnexpectedResponse:
        pass
    else:
        logger.info("Qdrant collection %r already exists; leaving it untouched", name)
        return False

    try:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_size, distance=DISTANCE),
        )
    except UnexpectedResponse:
        # 409 from a replica that got there first. Anything else re-raises after
        # the existence check below fails.
        if not _exists(client, name):
            raise
        logger.info("Qdrant collection %r was created concurrently", name)
        return False

    logger.info("Created Qdrant collection %r (size=%d, distance=%s)", name, vector_size, DISTANCE)
    return True


def _exists(client: QdrantClient, name: str) -> bool:
    """Return whether the collection is present, without raising."""
    try:
        client.get_collection(name)
    except UnexpectedResponse:
        return False
    return True


def check_health(client: QdrantClient) -> bool:
    """Return whether Qdrant answers. Used by ``GET /health``.

    Any failure is a failure — the health endpoint reports a single ok/error
    flag per dependency and the exception detail goes to the log, not to the
    response body.
    """
    try:
        client.get_collections()
    except Exception:  # any transport or server fault is "not ok"
        logger.warning("Qdrant health check failed", exc_info=True)
        return False
    return True


def ensure_payload_indexes(client: QdrantClient, name: str) -> tuple[str, ...]:
    """Create the payload indexes that are missing, and leave the rest alone.

    Returns the fields this call created, so a caller can log the difference
    rather than "ran the indexer" on every boot.

    Same get-or-create shape as :func:`ensure_collection`, and for a related
    reason: ``create_payload_index`` on an existing field is not free — it
    rebuilds the index over the whole collection — so calling it unconditionally
    on every startup would make each rollout re-index every policy chunk.
    """
    existing = _indexed_fields(client, name)
    created: list[str] = []

    for field in INDEXED_PAYLOAD_FIELDS:
        if field in existing:
            continue
        client.create_payload_index(
            collection_name=name,
            field_name=field,
            field_schema=PayloadSchemaType.KEYWORD,
        )
        created.append(field)

    if created:
        logger.info("Created Qdrant payload indexes on %s in %r", ", ".join(created), name)
    return tuple(created)


def _indexed_fields(client: QdrantClient, name: str) -> frozenset[str]:
    """Return the payload fields the collection already indexes.

    An unreachable collection yields the empty set rather than raising, so
    startup treats it the same way it treats an unreachable Qdrant: log, carry
    on, and let ``/health`` report the failure.
    """
    try:
        schema = client.get_collection(name).payload_schema
    except UnexpectedResponse:
        return frozenset()
    return frozenset(schema or {})


def point_id(policy_id: str, chunk_index: int) -> str:
    """Return the deterministic Qdrant point ID for one chunk of one policy.

    Derived from the policy and the chunk's position, so re-ingesting a document
    overwrites its own points rather than appending a parallel copy. The dedup
    logic in :mod:`track_b_rag.ingestion` deletes the old points first anyway;
    this makes a crash between the delete and the upsert self-healing instead of
    leaving duplicates that only a full re-index would clear.
    """
    return str(uuid.uuid5(POINT_NAMESPACE, f"{policy_id}:{chunk_index}"))


def build_points(
    *,
    policy_id: str,
    payer: str,
    plan_type: str | None,
    state: str | None,
    chunks: Sequence[str],
    vectors: Sequence[Sequence[float]],
) -> list[PointStruct]:
    """Pair chunks with their vectors into points carrying the payload schema.

    Args:
        policy_id: The payer's identifier for the document.
        payer: The **canonical payer slug**, not a display name — this is the
            value ``policy_query_filter`` matches by exact equality, so it has to
            be what a query's payer normalises to. See :mod:`payer_vocab`.
        plan_type: Plan type the policy applies to, or None for all.
        state: Two-letter state code, or None for a policy that applies
            nationally.
        chunks: The document's text chunks, in order.
        vectors: One embedding per chunk, in the same order.

    Raises:
        ValueError: The chunk and vector counts disagree, which would otherwise
            silently pair a chunk with another chunk's embedding.
    """
    if len(chunks) != len(vectors):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors — refusing to pair them")

    return [
        PointStruct(
            id=point_id(policy_id, index),
            vector=list(vector),
            payload={
                "policy_id": policy_id,
                "payer": payer,
                "plan_type": plan_type,
                "state": state,
                "chunk_index": index,
                "text": chunk,
            },
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
    ]


def policy_filter(policy_id: str) -> Filter:
    """Return the filter selecting every point belonging to one policy."""
    return Filter(must=[FieldCondition(key="policy_id", match=MatchValue(value=policy_id))])


def delete_policy_points(client: QdrantClient, name: str, policy_id: str) -> None:
    """Remove every indexed chunk of one policy.

    Runs before re-indexing a changed document. Deleting by filter rather than by
    reconstructed point ID matters when the new version has fewer chunks than the
    old one: addressing only the IDs the new version produces would strand the
    tail of the previous version in the collection, still retrievable and now
    contradicting the current policy text.
    """
    client.delete(
        collection_name=name,
        points_selector=FilterSelector(filter=policy_filter(policy_id)),
        wait=True,
    )
    logger.info("Deleted existing Qdrant points for policy %r in %r", policy_id, name)


def upsert_points(client: QdrantClient, name: str, points: Sequence[PointStruct]) -> None:
    """Write points into the collection, waiting until they are searchable.

    ``wait=True`` because the ingestion endpoint reports how many chunks it
    indexed: returning before the write is visible would make that count a claim
    about the near future rather than a fact, and the dedup tests assert on it.
    """
    if not points:
        return
    client.upsert(collection_name=name, points=list(points), wait=True)


def count_policy_points(client: QdrantClient, name: str, policy_id: str) -> int:
    """Return how many chunks the collection currently holds for one policy."""
    return client.count(
        collection_name=name,
        count_filter=policy_filter(policy_id),
        exact=True,
    ).count

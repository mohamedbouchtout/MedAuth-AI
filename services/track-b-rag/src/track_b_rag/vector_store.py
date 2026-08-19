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
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Distance, VectorParams

from track_b_rag.config import get_settings

logger = logging.getLogger(__name__)

#: Cosine, per TASK-010. bge embeddings are normalised, so cosine and dot
#: product rank identically — cosine is the one the ingestion and query tasks
#: were specified against.
DISTANCE = Distance.COSINE


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

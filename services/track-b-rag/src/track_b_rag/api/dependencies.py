"""Request-scoped dependencies for track-b-rag.

The Qdrant client is reached through a FastAPI dependency rather than imported
directly at the call site, so a test can substitute a fake through
``app.dependency_overrides`` without a Qdrant container in reach.
"""

from __future__ import annotations

from qdrant_client import QdrantClient

from track_b_rag.vector_store import get_client


async def get_qdrant() -> QdrantClient:
    """Return the process-wide Qdrant client."""
    return get_client()

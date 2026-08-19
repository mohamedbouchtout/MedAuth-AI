"""FastAPI application for track-b-rag.

Runs on port 8002 per the Local Development port table in CLAUDE.md::

    cd services/track-b-rag
    uv run uvicorn track_b_rag.main:app --reload --port 8002

Startup ensures the Qdrant collection exists and does not touch it if it
already does — see :mod:`track_b_rag.vector_store` for why that distinction is
the whole point. A Qdrant that is unreachable at startup is logged and
tolerated: the service still boots and ``GET /health`` answers 503 until the
container is up, which is more useful than a crash loop that hides the cause.

The embedding model is *not* loaded here. It is a lazy singleton, so the first
health probe or query pays for the load; loading 1.3 GB of weights before the
port opens would make every rollout look like a failure to the orchestrator.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from api_envelope import install_error_handlers
from track_b_rag.api.health import router as health_router
from track_b_rag.config import get_settings
from track_b_rag.embeddings import reset_embedder
from track_b_rag.vector_store import close_client, ensure_collection, get_client

logger = logging.getLogger(__name__)


async def initialize_vector_store() -> bool:
    """Create the policy collection if it is missing. Never destructive.

    Returns whether the collection is known to be present when this returns —
    False means Qdrant could not be reached, not that anything was deleted.
    """
    settings = get_settings()
    try:
        await run_in_threadpool(
            ensure_collection,
            get_client(),
            settings.qdrant_collection,
            settings.embedding_dimensions,
        )
    except Exception:
        logger.warning(
            "Could not reach Qdrant to ensure collection %r at startup; "
            "the service will start and report unhealthy until it is available",
            settings.qdrant_collection,
            exc_info=True,
        )
        return False
    return True


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Ensure the collection on startup; release the client and model on shutdown."""
    await initialize_vector_store()
    yield
    close_client()
    reset_embedder()


def create_app() -> FastAPI:
    """Build the application. A factory so tests get an isolated instance."""
    app = FastAPI(
        title="MedAuth AI — track-b-rag",
        description=(
            "Insurance policy retrieval-augmented generation. Owns the Qdrant "
            "insurance_policies collection and the local embedding model."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(health_router)
    return app


app = create_app()

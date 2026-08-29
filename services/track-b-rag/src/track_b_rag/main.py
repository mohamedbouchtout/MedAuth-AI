"""FastAPI application for track-b-rag.

Runs on port 8002 per the Local Development port table in CLAUDE.md::

    cd services/track-b-rag
    uv run uvicorn track_b_rag.main:app --reload --port 8002

Startup ensures the Qdrant collection and its payload indexes exist, and
touches neither if they already do — see :mod:`track_b_rag.vector_store` for why
that distinction is the whole point. A Qdrant that is unreachable at startup is logged and
tolerated: the service still boots and ``GET /health`` answers 503 until the
container is up, which is more useful than a crash loop that hides the cause.

The embedding model is *not* loaded here. It is a lazy singleton, so the first
health probe or query pays for the load; loading 1.3 GB of weights before the
port opens would make every rollout look like a failure to the orchestrator.

Startup also launches the transcript consumer (TASK-021), which watches the
Redis bus for the length of the process. It is started rather than awaited: its
whole life is a read loop, and a Redis that is unreachable at boot is handled
inside that loop by retrying, for the same reason an unreachable Qdrant does not
stop the service booting. ``GET /health`` is where either one becomes visible.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from api_envelope import install_error_handlers
from cors_policy import install_cors
from track_b_rag.api.health import router as health_router
from track_b_rag.api.nudges import router as nudges_router
from track_b_rag.api.policies import router as policies_router
from track_b_rag.api.query import router as query_router
from track_b_rag.bedrock import reset_clients as reset_bedrock_clients
from track_b_rag.cache import close_client as close_redis
from track_b_rag.cache import get_client as get_redis_client
from track_b_rag.config import get_settings
from track_b_rag.db import dispose_engine
from track_b_rag.embeddings import reset_embedder
from track_b_rag.transcript_consumer import TranscriptConsumer
from track_b_rag.vector_store import (
    close_client,
    ensure_collection,
    ensure_payload_indexes,
    get_client,
)

logger = logging.getLogger(__name__)


async def initialize_vector_store() -> bool:
    """Create the policy collection and its payload indexes if they are missing.

    Never destructive: both steps are get-or-create, so a restart against a
    populated collection leaves every indexed policy and every existing index
    exactly as it found them.

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
        # After the collection, not before: an index cannot be created on a
        # collection that does not exist yet.
        await run_in_threadpool(
            ensure_payload_indexes,
            get_client(),
            settings.qdrant_collection,
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
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Ensure the collection and start the consumer; release everything on shutdown.

    The consumer is stashed on ``app.state`` rather than in a module global so
    that it belongs to the application that started it — ``GET /health`` reads
    it back through a dependency, and a test that builds an app without this
    lifespan correctly finds none.
    """
    await initialize_vector_store()
    consumer = TranscriptConsumer(get_redis_client())
    consumer.start()
    app.state.transcript_consumer = consumer

    yield

    # Before the Redis client closes: the consumer holds a pub/sub connection on
    # it and cancelling it afterwards would unwind against a closed pool.
    await consumer.stop()
    close_client()
    reset_embedder()
    reset_bedrock_clients()
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    """Build the application. A factory so tests get an isolated instance."""
    app = FastAPI(
        title="MedAuth AI — track-b-rag",
        description=(
            "Insurance policy retrieval-augmented generation. Owns the Qdrant "
            "insurance_policies collection and the local embedding model, and "
            "answers prior authorization questions during a live encounter."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    # TASK-041c. The acknowledge route below is this service's only browser
    # caller; the policy itself is settled repo-wide in CLAUDE.md, "CORS and
    # browser reachability", and configured per environment.
    install_cors(app, get_settings().cors_allowed_origins)
    app.include_router(health_router)
    app.include_router(policies_router)
    # Two routers share the /policies prefix. The ingest route writes no audit
    # row and the query route must — keeping them in separate modules keeps that
    # difference, and the tests that assert it, unambiguous.
    app.include_router(query_router)
    # The first route outside /policies, and the first one a browser calls
    # (TASK-041b). Reachable from apps/web via the CORS policy installed above
    # (TASK-041c), given an origin configured for the environment.
    app.include_router(nudges_router)
    return app


app = create_app()

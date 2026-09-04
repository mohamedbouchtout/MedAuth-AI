"""FastAPI application for track-a-clinical.

The first HTTP surface in the monorepo. Runs on port 8003 per the Local
Development port table in CLAUDE.md::

    cd services/track-a-clinical
    uv run uvicorn track_a_clinical.main:app --reload --port 8003

The module sits inside the ``track_a_clinical`` package rather than at
``src/main.py``: this service ships a named package precisely so other services
can import its models, and a top-level ``src`` module here would shadow the one
every other service still installs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api_envelope import install_error_handlers
from cors_policy import install_cors
from track_a_clinical.api.dependencies import close_redis, get_redis
from track_a_clinical.api.health import router as health_router
from track_a_clinical.api.notes import router as notes_router
from track_a_clinical.api.prior_auth import router as prior_auth_router
from track_a_clinical.api.providers import router as providers_router
from track_a_clinical.api.sessions import router as sessions_router
from track_a_clinical.bedrock import reset_clients
from track_a_clinical.config import get_settings
from track_a_clinical.consumer import TranscriptConsumer
from track_a_clinical.db import dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the transcript consumer, and release everything on shutdown.

    The database pool and the Redis client are still opened lazily on first use,
    so the service starts even when a backing store is briefly unreachable — the
    consumer's own read loop reconnects rather than failing startup.

    The consumer is stashed on ``app.state`` rather than in a module global so
    ``GET /health`` can reach the instance this app owns, and so a test app
    built without this lifespan simply has none.
    """
    consumer = TranscriptConsumer(await get_redis())
    consumer.start()
    app.state.transcript_consumer = consumer
    yield
    # Before the Redis client closes: the consumer holds a pub/sub connection on
    # it, and any generation still running is cancelled here rather than left to
    # write into a closing pool.
    await consumer.stop()
    await dispose_engine()
    await close_redis()
    reset_clients()


def create_app() -> FastAPI:
    """Build the application. A factory so tests get an isolated instance."""
    app = FastAPI(
        title="MedAuth AI — track-a-clinical",
        description=(
            "SOAP note generation and session lifecycle. Owns the encounters "
            "table and is the only issuer of session JWTs."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    # TASK-041c. apps/web calls POST /sessions/start and the note routes from a
    # browser; the policy is settled repo-wide in CLAUDE.md, "CORS and browser
    # reachability", and configured per environment.
    install_cors(app, get_settings().cors_allowed_origins)
    app.include_router(health_router)
    app.include_router(sessions_router)
    app.include_router(notes_router)
    app.include_router(prior_auth_router)
    app.include_router(providers_router)
    return app


app = create_app()

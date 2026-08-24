"""FastAPI application for audio-ingestion.

Runs on port 8001 per the Local Development port table in CLAUDE.md::

    cd services/audio-ingestion
    uv run uvicorn src.main:app --reload --port 8001

The module is ``src.main`` rather than a named package, unlike
``track_a_clinical`` and ``track_b_rag``. Those two ship named packages because
another service imports from them; nothing imports this one — it communicates
only by publishing to Redis — so it keeps the bare ``src`` layout every other
service still has. The task that first needs to import from here is the task
that should rename it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api_envelope import install_error_handlers
from src.api.dependencies import close_redis
from src.api.health import router as health_router
from src.api.websocket import router as websocket_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Release the Redis client on shutdown.

    Nothing is opened on startup: Redis connects lazily on first command and a
    Transcribe stream belongs to a connection rather than to the process, so the
    service starts even when a backing service is briefly unreachable.
    """
    yield
    await close_redis()


def create_app() -> FastAPI:
    """Build the application. A factory so tests get an isolated instance."""
    app = FastAPI(
        title="MedAuth AI — audio-ingestion",
        description=(
            "Streams encounter audio from the clinical apps to AWS Transcribe "
            "Medical and publishes transcript segments to Redis. Audio is held "
            "in memory only and never written to disk."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(websocket_router)
    return app


app = create_app()

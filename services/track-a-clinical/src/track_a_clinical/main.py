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
from track_a_clinical.api.dependencies import close_redis
from track_a_clinical.api.sessions import router as sessions_router
from track_a_clinical.db import dispose_engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Release the database pool and Redis client on shutdown.

    Nothing is opened on startup: both connect lazily on first use, so the
    service starts even when a backing store is briefly unreachable.
    """
    yield
    await dispose_engine()
    await close_redis()


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
    app.include_router(sessions_router)
    return app


app = create_app()

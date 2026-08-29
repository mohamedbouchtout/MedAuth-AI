"""FastAPI application for nudge-service.

Runs on port 8005 per the Local Development port table in CLAUDE.md::

    cd services/nudge-service
    uv run uvicorn src.main:app --reload --port 8005

The module is ``src.main`` rather than a named package, like every service except
``track_a_clinical`` and ``track_b_rag``. Those two ship named packages because
another service imports from them; nothing imports this one — it reads from Redis
and writes to a socket — so it keeps the bare ``src`` layout. The task that first
needs to import from here is the task that should rename it.
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
    subscription belongs to a connection rather than to the process, so the
    service starts even when the bus is briefly unreachable.
    """
    yield
    await close_redis()


def create_app() -> FastAPI:
    """Build the application. A factory so tests get an isolated instance."""
    app = FastAPI(
        title="MedAuth AI — nudge-service",
        description=(
            "Relays clinical nudges from the Redis bus to connected clients over "
            "a WebSocket. Payloads are forwarded exactly as published."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(websocket_router)
    return app


app = create_app()

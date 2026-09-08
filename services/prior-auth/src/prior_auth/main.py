"""FastAPI application for prior-auth.

Runs on the port CLAUDE.md's local development table will carry for this
service::

    cd services/prior-auth
    uv run uvicorn prior_auth.main:app --reload --port 8007

**The HTTP surface is deliberately just ``/health``.** This service's work
arrives on a Redis subscription, not on a request — TASK-061 adds the first real
route. The application exists so the consumer has a lifespan to run in and a
readiness probe to be observed through, which is what stops a dead consumer
looking like a quiet afternoon.

No CORS middleware: nothing here answers a browser. Per CLAUDE.md's "CORS and
browser reachability", it is installed when a service grows a browser-facing
HTTP route, not pre-emptively.

The module sits inside the ``prior_auth`` package rather than at
``src/main.py``: this service imports track-a-clinical's mapped classes, and a
top-level ``src`` module here would shadow the one other services still install.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api_envelope import install_error_handlers
from prior_auth.api.dependencies import close_redis, get_redis
from prior_auth.api.health import router as health_router
from prior_auth.consumer import SessionEndConsumer
from prior_auth.db import dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the session-end consumer, and release everything on shutdown.

    The database pool and the Redis client are still opened lazily on first use,
    so the service starts even when a backing store is briefly unreachable — the
    consumer's own read loop reconnects rather than failing startup.

    The consumer is stashed on ``app.state`` rather than in a module global so
    ``GET /health`` can reach the instance this app owns, and so a test app
    built without this lifespan simply has none.
    """
    consumer = SessionEndConsumer(await get_redis())
    consumer.start()
    app.state.session_end_consumer = consumer
    yield
    # Before the Redis client closes: the consumer holds a pub/sub connection on
    # it, and any assembly still running is cancelled here rather than left to
    # write into a closing pool.
    await consumer.stop()
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    """Build the application. A factory so tests get an isolated instance."""
    app = FastAPI(
        title="MedAuth AI — prior-auth",
        description=(
            "Prior authorization bundle assembly. Subscribes to session-end "
            "signals and records one request per encounter that flagged a "
            "coded procedure."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(health_router)
    return app


app = create_app()

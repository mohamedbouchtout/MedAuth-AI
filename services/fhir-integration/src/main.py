"""FastAPI application for fhir-integration.

Runs on port 8004 per the Local Development port table in CLAUDE.md::

    cd services/fhir-integration
    uv run uvicorn src.main:app --reload --port 8004

The module is ``src.main`` rather than a named package, like every service
except ``track_a_clinical`` and ``track_b_rag``. Those two ship named packages
because another service imports from them; nothing imports this one, so it keeps
the bare ``src`` layout. The task that first needs to import from here is the
task that should rename it.

**CORS is installed, as of TASK-052.** CLAUDE.md's rule is that a service
installs ``cors_policy`` when it grows a browser-facing HTTP route, and not
pre-emptively — and TASK-052 is that moment: ``GET /fhir/patient/{id}/context``
is a real cross-origin fetch from ``apps/web``. The two launch routes are still
not the reason. They are browser *navigations* — the EHR redirects the browser
to ``/fhir/launch`` and the authorization server redirects it to
``/fhir/callback`` — and a browser applies no CORS to a top-level navigation,
exactly as it applies none to a WebSocket upgrade.

The ``X-MedAuth-Launch-Id`` header the FHIR routes read is a custom request
header, so a browser preflights it; it is allowed in ``packages/cors-policy``
rather than here, because that package fixes the header list for the whole
repository and a service that could pass its own would be the per-service policy
TASK-041c refused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api_envelope import install_error_handlers
from cors_policy import install_cors
from src.api.dependencies import close_clients
from src.api.fhir import router as fhir_router
from src.api.health import router as health_router
from src.api.smart import router as smart_router
from src.config import get_settings


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Release the Redis and HTTP clients on shutdown.

    Nothing is opened on startup: Redis connects lazily on first command and the
    HTTP client opens a connection per request, so the service starts even when
    a dependency is briefly unreachable.
    """
    yield
    await close_clients()


def create_app() -> FastAPI:
    """Build the application. A factory so tests get an isolated instance."""
    app = FastAPI(
        title="MedAuth AI — fhir-integration",
        description=(
            "SMART on FHIR launch and the multi-EHR adapter layer. Exchanges an "
            "EHR's launch for an access token, which later tasks use to read and "
            "write FHIR resources through a vendor-appropriate adapter."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    # Origins are per environment; the policy itself — methods, headers,
    # credentials — is settled repo-wide in CLAUDE.md, "CORS and browser
    # reachability", and lives in packages/cors-policy.
    install_cors(app, get_settings().cors_allowed_origins)
    app.include_router(health_router)
    app.include_router(smart_router)
    app.include_router(fhir_router)
    return app


app = create_app()

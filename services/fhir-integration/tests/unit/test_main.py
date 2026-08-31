"""The application factory: what it installs, and what it deliberately does not."""

from __future__ import annotations

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from src.api import dependencies
from src.main import create_app


def test_the_factory_returns_isolated_instances() -> None:
    assert create_app() is not create_app()


def test_every_router_is_mounted() -> None:
    """Asserted through the generated schema rather than ``app.routes``.

    This FastAPI version keeps an included router's routes behind an opaque
    wrapper, and the schema is what a client sees regardless.
    """
    paths = set(create_app().openapi()["paths"])

    assert {
        "/health",
        "/fhir/launch",
        "/fhir/callback",
        "/fhir/patient/{patient_id}/context",
        "/fhir/encounter/{encounter_id}",
    } == paths


def test_the_shared_error_handlers_are_installed() -> None:
    """A 404 must come back in the envelope, not FastAPI's own {"detail": ...}."""
    with TestClient(create_app()) as client:
        body = client.get("/no-such-route").json()

    assert body["data"] is None
    assert set(body["error"]) == {"code", "message"}


def test_no_cors_middleware_is_installed() -> None:
    """Deliberate, and worth asserting so it is a decision rather than an omission.

    Both routes here are browser *navigations* — the EHR redirects to
    `/fhir/launch`, the authorization server redirects to `/fhir/callback` — and
    a browser applies no CORS to a top-level navigation, exactly as it applies
    none to a WebSocket upgrade. CLAUDE.md's rule is to install the shared policy
    when a service grows a browser-facing HTTP route, not pre-emptively.
    TASK-052's `GET /fhir/patient/{id}/context` is a real cross-origin fetch from
    apps/web and should install it in the same change that adds the route.
    """
    installed = [middleware.cls for middleware in create_app().user_middleware]

    assert CORSMiddleware not in installed


@pytest.mark.asyncio
async def test_shutdown_releases_both_clients() -> None:
    """A leaked HTTP client holds sockets open to every EHR the pod has launched."""
    redis = dependencies._redis_client()
    http = dependencies._http_client()

    await dependencies.close_clients()

    assert redis.connection_pool is not None  # closed, not discarded mid-use
    assert http.is_closed
    assert dependencies._redis_client.cache_info().currsize == 0
    assert dependencies._http_client.cache_info().currsize == 0

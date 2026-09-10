"""The CORS policy is actually installed on this service's app, and covers the
routes a browser calls.

``packages/cors-policy`` has its own tests for what the policy does. What can
only be checked here is that this service wires it up and that the specific
route-and-method combinations a browser uses fall inside the policy's fixed
methods and headers — a service that imported the package and forgot the call
would pass every test in that package and still refuse every browser, and a
route added outside the allowed methods would fail the same way with the call in
place.

**That second half is why this file exists rather than resting on
``install_cors()`` being present.** TASK-052 installed the policy for
``GET /fhir/patient/{id}/context``; TASK-051f added ``POST /fhir/launch/claim``,
which is a different method on a different path, and the installation site says
nothing about whether the policy admits it. The failure mode of assuming
otherwise is a preflight rejected in a browser console belonging to whoever
consumes the route next, which is a later task and a different app.

The two launch routes are deliberately not covered: they are top-level browser
navigations — the EHR redirects to ``/fhir/launch``, the authorization server to
``/fhir/callback`` — and a browser applies no CORS to a navigation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from src.config import get_settings
from src.main import create_app

ALLOWED_ORIGIN = "https://app.example.com"
OTHER_ORIGIN = "https://evil.example.com"

CLAIM_URL = "/fhir/launch/claim"


def patient_context_url() -> str:
    return f"/fhir/patient/{uuid.uuid4()}/context"


@pytest.fixture
def configured_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client for an app configured with exactly one allowed origin.

    The lifespan is deliberately not entered: a preflight is answered by
    middleware before routing, so this needs no Redis and no EHR.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ALLOWED_ORIGIN)
    yield TestClient(create_app())
    get_settings.cache_clear()


@pytest.fixture
def unconfigured_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    get_settings.cache_clear()
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    yield TestClient(create_app())
    get_settings.cache_clear()


def test_the_claim_route_preflight_is_answered_for_a_configured_origin(
    configured_client: TestClient,
) -> None:
    """TASK-051f's own acceptance: this route and this method, not the policy in general."""
    response = configured_client.options(
        CLAIM_URL,
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]


def test_the_claim_route_preflight_admits_a_json_body(
    configured_client: TestClient,
) -> None:
    """The claim travels in a JSON body, which is what makes it preflight at all."""
    response = configured_client.options(
        CLAIM_URL,
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200, response.text
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "content-type" in allowed


def test_the_chart_read_preflight_admits_the_launch_id_header(
    configured_client: TestClient,
) -> None:
    """TASK-052's route, kept covered here so one file answers "is CORS right"."""
    response = configured_client.options(
        patient_context_url(),
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-medauth-launch-id",
        },
    )

    assert response.status_code == 200, response.text
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "x-medauth-launch-id" in allowed


def test_an_unlisted_origin_is_refused_on_the_claim_route(
    configured_client: TestClient,
) -> None:
    response = configured_client.options(
        CLAIM_URL,
        headers={"Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-origin" not in response.headers


def test_an_unconfigured_deployment_answers_no_browser(
    unconfigured_client: TestClient,
) -> None:
    """The complementary half: an empty origin list installs no middleware at all.

    Without this, a test asserting a configured origin works would also pass
    against a permissive default, which is the thing worth guarding against.
    """
    response = unconfigured_client.options(
        CLAIM_URL,
        headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-origin" not in response.headers

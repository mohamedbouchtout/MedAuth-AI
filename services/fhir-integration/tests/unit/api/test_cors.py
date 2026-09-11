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

TASK-070 is that later task, and it makes two more routes browser-facing for the
first time: ``GET /fhir/launch-context`` and ``GET /fhir/patient/search``. Both
have been called since TASK-025b — but only from ``apps/mobile``, which is not a
browser and applies no CORS to anything, so neither had ever been preflighted by
anyone. "It already works from mobile" is precisely the reasoning this file
exists to refuse.

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
LAUNCH_CONTEXT_URL = "/fhir/launch-context"
PATIENT_SEARCH_URL = "/fhir/patient/search"


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


def test_the_launch_context_preflight_admits_the_launch_id_header(
    configured_client: TestClient,
) -> None:
    """TASK-070's first call after redeeming a claim, and the first from a browser.

    The launch travels in ``X-MedAuth-Launch-Id``, which is a header a browser
    does not send by default — so this route preflights, and a policy that did
    not list the header would leave the web app unable to identify any patient at
    all while the same call kept working from ``apps/mobile``.
    """
    response = configured_client.options(
        LAUNCH_CONTEXT_URL,
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-medauth-launch-id",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "x-medauth-launch-id" in response.headers["access-control-allow-headers"].lower()


def test_the_patient_search_preflight_admits_the_launch_id_header(
    configured_client: TestClient,
) -> None:
    """The standalone-launch half of TASK-070's patient identification.

    A different path from the launch context and reached only when the EHR named
    nobody, so a policy covering one and not the other would fail exactly for the
    providers who opened MedAuth directly.
    """
    response = configured_client.options(
        PATIENT_SEARCH_URL,
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-medauth-launch-id",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "x-medauth-launch-id" in response.headers["access-control-allow-headers"].lower()


def test_an_unlisted_origin_is_refused_on_the_launch_context_route(
    configured_client: TestClient,
) -> None:
    """The complementary half, per route rather than once for the service.

    A preflight answered for the right origin proves nothing on its own if the
    policy would answer for any origin, and the allow-list is applied by one
    middleware — so this is cheap, and it is what makes the test above mean
    something.
    """
    response = configured_client.options(
        LAUNCH_CONTEXT_URL,
        headers={"Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in response.headers

"""The CORS policy is actually installed on this service's app (TASK-041c).

``packages/cors-policy`` has its own tests for what the policy does. What can
only be checked here is that this service wires it up, reading the origins from
its own settings — a service that imported the package and forgot the call would
pass every test in that package and still refuse every browser.

The route exercised is ``PATCH /nudges/{nudge_id}/acknowledge`` (TASK-041b),
which is the browser caller that made this necessary.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from track_b_rag.config import get_settings
from track_b_rag.main import create_app

ALLOWED_ORIGIN = "https://app.example.com"
OTHER_ORIGIN = "https://evil.example.com"


def acknowledge_url() -> str:
    return f"/nudges/{uuid.uuid4()}/acknowledge"


@pytest.fixture
def configured_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client for an app configured with exactly one allowed origin.

    The lifespan is deliberately not entered: a preflight is answered by
    middleware before routing, so this needs no Qdrant, no Redis and no
    consumer.
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


def test_preflight_from_a_configured_origin_is_answered_with_patch(
    configured_client: TestClient,
) -> None:
    """The acceptance criterion: the dismiss button's preflight gets an answer."""
    response = configured_client.options(
        acknowledge_url(),
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "PATCH" in response.headers["access-control-allow-methods"]
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


def test_preflight_from_another_origin_is_not_granted(configured_client: TestClient) -> None:
    response = configured_client.options(
        acknowledge_url(),
        headers={"Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "PATCH"},
    )

    assert "access-control-allow-origin" not in response.headers


def test_the_origins_come_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
    configured_client: TestClient,
) -> None:
    """A second app built from a different value grants a different origin.

    A hardcoded list would make these two agree, which is what this asserts
    against.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", OTHER_ORIGIN)
    other_client = TestClient(create_app())
    get_settings.cache_clear()

    preflight = {"Access-Control-Request-Method": "PATCH"}
    headers = {"Origin": OTHER_ORIGIN, **preflight}
    first = configured_client.options(acknowledge_url(), headers=headers)
    second = other_client.options(acknowledge_url(), headers=headers)

    assert "access-control-allow-origin" not in first.headers
    assert second.headers["access-control-allow-origin"] == OTHER_ORIGIN


def test_no_configured_origins_answers_no_browser(unconfigured_client: TestClient) -> None:
    response = unconfigured_client.options(
        acknowledge_url(),
        headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "PATCH"},
    )

    assert "access-control-allow-origin" not in response.headers


def test_credentials_are_never_granted(configured_client: TestClient) -> None:
    """Nothing here authenticates with a cookie, and the WebSocket origin
    reasoning in CLAUDE.md depends on that staying true."""
    response = configured_client.options(
        acknowledge_url(),
        headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "PATCH"},
    )

    assert "access-control-allow-credentials" not in response.headers

"""The CORS policy is actually installed on this service's app (TASK-041c).

``packages/cors-policy`` tests what the policy does; this tests that this
service wires it up from its own settings. A service that imported the package
and forgot the call would pass every test over there and still refuse every
browser.

The routes exercised are the ones ``apps/web`` calls: ``POST /sessions/start``
when a provider taps "start visit", and ``PATCH /notes/{session_id}`` from the
review screen.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from track_a_clinical.config import get_settings
from track_a_clinical.main import create_app

ALLOWED_ORIGIN = "https://app.example.com"
OTHER_ORIGIN = "https://evil.example.com"

#: 34 characters, over the 32-byte floor the issuer enforces. Settings will not
#: construct without it, and this suite never mints a token.
SIGNING_KEY = "track-a-cors-test-signing-key-32by"


@pytest.fixture
def configured_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client for an app configured with exactly one allowed origin.

    The lifespan is deliberately not entered: a preflight is answered by
    middleware before routing, so this needs no Redis and no consumer.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ALLOWED_ORIGIN)
    yield TestClient(create_app())
    get_settings.cache_clear()


def test_preflight_for_starting_a_session_is_answered(configured_client: TestClient) -> None:
    response = configured_client.options(
        "/sessions/start",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]


def test_preflight_for_editing_a_note_is_answered(configured_client: TestClient) -> None:
    response = configured_client.options(
        f"/notes/{uuid.uuid4()}",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert "PATCH" in response.headers["access-control-allow-methods"]


def test_an_origin_outside_the_configured_list_is_not_granted(
    configured_client: TestClient,
) -> None:
    response = configured_client.options(
        "/sessions/start",
        headers={"Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-origin" not in response.headers


def test_the_origins_come_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
    configured_client: TestClient,
) -> None:
    """A second app built from a different value grants a different origin."""
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", OTHER_ORIGIN)
    other_client = TestClient(create_app())
    get_settings.cache_clear()

    preflight = {"Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "POST"}

    assert (
        "access-control-allow-origin"
        not in configured_client.options("/sessions/start", headers=preflight).headers
    )
    assert (
        other_client.options("/sessions/start", headers=preflight).headers[
            "access-control-allow-origin"
        ]
        == OTHER_ORIGIN
    )


def test_no_configured_origins_answers_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    client = TestClient(create_app())
    get_settings.cache_clear()

    response = client.options(
        "/sessions/start",
        headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-origin" not in response.headers

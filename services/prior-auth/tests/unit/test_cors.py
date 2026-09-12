"""The CORS policy is actually installed on this service's app (TASK-072).

``packages/cors-policy`` tests what the policy does; this tests that this
service wires it up from its own settings, and that the one route a browser
calls is genuinely answered. A service that imported the package and forgot the
call would pass every test over there and still refuse every browser.

**This file exists because installing the middleware is not covering a route.**
The policy's methods and headers are fixed repo-wide, so a service can import
the package, call ``install_cors``, and still refuse a browser on a route whose
method the policy does not list. The first two cases here moved out of
``test_app.py``, where they asserted only that ``CORSMiddleware`` appeared in
the middleware stack — true, necessary, and not the same claim as "a browser can
call the submit route". The rest are that claim.

The route exercised is ``POST /prior-auth/{request_id}/submit``, which
TASK-072's dashboard calls to resubmit a denied or errored request. It is the
only browser-facing route this service has; assembly still arrives on a Redis
subscription rather than over HTTP.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from prior_auth.config import get_settings
from prior_auth.main import create_app

ALLOWED_ORIGIN = "https://app.example.com"
OTHER_ORIGIN = "https://evil.example.com"


@pytest.fixture
def configured_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client for an app configured with exactly one allowed origin.

    The lifespan is deliberately not entered: a preflight is answered by
    middleware before routing, so this needs no Redis, no database and no
    consumer.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ALLOWED_ORIGIN)
    client = TestClient(create_app())
    get_settings.cache_clear()
    return client


def test_cors_is_installed_when_an_origin_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverted by TASK-061, deliberately rather than by coincidence.

    This asserted the *absence* of CORS while nothing here answered a browser,
    which was correct until TASK-072's dashboard got a route to call: it
    resubmits a denied request through the submission router. Per CLAUDE.md,
    CORS is installed when a service grows a browser-facing HTTP route — so what
    changed is the condition the old assertion rested on, not the rule.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ALLOWED_ORIGIN)
    try:
        app = create_app()
        assert any("CORSMiddleware" in str(m.cls) for m in app.user_middleware)
    finally:
        get_settings.cache_clear()


def test_cors_trusts_no_origin_by_default() -> None:
    """An unconfigured deployment answers no browser rather than one nobody chose.

    ``install_cors`` adds nothing at all for an empty allow-list, which is why
    the assertion above has to configure one — and why this is the half that
    actually guards against a permissive default shipping.
    """
    get_settings.cache_clear()
    try:
        assert get_settings().cors_allowed_origins == ()
        app = create_app()
        assert not any("CORSMiddleware" in str(m.cls) for m in app.user_middleware)
    finally:
        get_settings.cache_clear()


def test_preflight_for_resubmitting_is_answered(configured_client: TestClient) -> None:
    """The claim the middleware assertion above cannot make on its own.

    A dashboard that lists denied requests and cannot resubmit one is the whole
    feature failing at its last step, and it would present as a broken button
    rather than as a policy that does not list this path and method.
    """
    response = configured_client.options(
        f"/prior-auth/{uuid.uuid4()}/submit",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]


def test_an_origin_outside_the_configured_list_is_not_granted(
    configured_client: TestClient,
) -> None:
    """The counterpart every case needs.

    A preflight answered for the right origin proves nothing on its own if the
    policy would answer for any origin — and this route transmits to a payer.
    """
    response = configured_client.options(
        f"/prior-auth/{uuid.uuid4()}/submit",
        headers={"Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-origin" not in response.headers


def test_the_origins_come_from_configuration(
    monkeypatch: pytest.MonkeyPatch, configured_client: TestClient
) -> None:
    """A second app built from a different value grants a different origin.

    What this adds over the two above: it shows the allow-list is read from
    ``CORS_ALLOWED_ORIGINS`` per environment rather than hardcoded to whatever
    the first test happened to set.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", OTHER_ORIGIN)
    other_client = TestClient(create_app())
    get_settings.cache_clear()

    preflight = {"Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "POST"}
    path = f"/prior-auth/{uuid.uuid4()}/submit"

    assert (
        "access-control-allow-origin"
        not in configured_client.options(path, headers=preflight).headers
    )
    assert (
        other_client.options(path, headers=preflight).headers["access-control-allow-origin"]
        == OTHER_ORIGIN
    )

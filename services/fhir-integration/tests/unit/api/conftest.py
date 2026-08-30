"""Fakes that let a whole SMART launch run with no Redis and no EHR.

Two fakes, both deliberately narrow.

``FakeRedis`` models exactly the four operations ``src.smart.store`` uses —
``set`` with an expiry, ``get``, ``getdel`` and ``ping``. ``getdel`` is the one
whose behaviour actually matters to a test: it is what makes a ``state``
single-use, so it returns a value once and ``None`` thereafter.

``FakeAuthorizationServer`` stands in for an EHR. It serves a discovery document
and a token endpoint, and it **checks PKCE the way a real authorization server
does** — recording the ``code_challenge`` from the authorization request and
refusing a token exchange whose ``code_verifier`` does not hash to it. A fake
that accepted any verifier would let the flow pass while sending PKCE nobody
verified, which is the failure the PKCE tests exist to catch.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.dependencies import get_http_client, get_redis
from src.config import get_settings
from src.main import create_app

#: The issuer used by tests that do not care which vendor answers. Its host
#: contains no vendor fragment, so it resolves to EHRType.GENERIC.
GENERIC_ISS = "https://fhir.example-hospital.org/r4"

#: An issuer whose host names a vendor, for the tests that care.
ATHENA_ISS = "https://api.platform.athenahealth.com/fhir/r4"

AUTHORIZATION_ENDPOINT = "https://auth.example-hospital.org/authorize"
TOKEN_ENDPOINT = "https://auth.example-hospital.org/token"

REDIRECT_URI = "https://app.medauth.test/fhir/callback"


class FakeRedis:
    """An in-memory stand-in for the operations ``src.smart.store`` uses."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int | None] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        self.expiries[key] = ex

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def getdel(self, key: str) -> str | None:
        """Read and delete in one step, which is what makes a state single-use."""
        self.expiries.pop(key, None)
        return self.values.pop(key, None)

    async def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("redis unreachable")
        return True


class FakeAuthorizationServer:
    """An EHR's discovery document and token endpoint, over an httpx transport."""

    def __init__(
        self,
        *,
        discovery_status: int = 200,
        discovery_body: dict[str, Any] | str | None = None,
        token_status: int = 200,
        token_body: dict[str, Any] | None = None,
    ) -> None:
        self.discovery_status = discovery_status
        self.discovery_body = discovery_body
        self.token_status = token_status
        self.token_body = token_body
        #: The code_challenge last seen on an authorization request, set by a
        #: test through ``observe_authorization()``.
        self.code_challenge: str | None = None
        #: Every form posted to the token endpoint, for assertions.
        self.token_requests: list[dict[str, str]] = []
        #: Authorization headers seen at the token endpoint.
        self.token_auth_headers: list[str | None] = []

    def observe_authorization(self, redirect_url: str) -> dict[str, str]:
        """Record what a redirect asked for, as the authorization server would.

        Returns the parsed query parameters so a test can assert on them.
        """
        query = parse_qs(urlsplit(redirect_url).query)
        params = {key: values[0] for key, values in query.items()}
        self.code_challenge = params.get("code_challenge")
        return params

    def _verifier_matches(self, code_verifier: str) -> bool:
        """Check a verifier against the recorded challenge, as RFC 7636 says."""
        if self.code_challenge is None:
            return True
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return expected == self.code_challenge

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer discovery and token requests; 404 anything else."""
        if request.url.path.endswith(".well-known/smart-configuration"):
            body = self.discovery_body
            if body is None:
                body = {
                    "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                    "token_endpoint": TOKEN_ENDPOINT,
                }
            if isinstance(body, str):
                return httpx.Response(self.discovery_status, text=body)
            return httpx.Response(self.discovery_status, json=body)

        if str(request.url) == TOKEN_ENDPOINT:
            form = {
                key: value
                for key, value in parse_qs(request.content.decode()).items()
                for value in [value[0]]
            }
            self.token_requests.append(form)
            self.token_auth_headers.append(request.headers.get("authorization"))

            if not self._verifier_matches(form.get("code_verifier", "")):
                return httpx.Response(400, json={"error": "invalid_grant"})

            body = self.token_body
            if body is None:
                body = {
                    "access_token": "ehr-access-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "ehr-refresh-token",
                    "scope": "launch user/*.read",
                    "patient": "Patient/synthea-123",
                }
            return httpx.Response(self.token_status, json=body)

        return httpx.Response(404, json={"error": "not_found"})

    def client(self) -> httpx.AsyncClient:
        """Return an httpx client whose requests this fake answers."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def smart_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure a deployment with a generic and an Athena registration."""
    get_settings.cache_clear()
    monkeypatch.setenv("SMART_REDIRECT_URI", REDIRECT_URI)
    monkeypatch.setenv("SMART_SCOPES", "openid fhirUser user/*.read")
    monkeypatch.setenv("SMART_LAUNCH_TTL_SECONDS", "600")
    monkeypatch.setenv("GENERIC_CLIENT_ID", "medauth-generic-client")
    monkeypatch.setenv("GENERIC_CLIENT_SECRET", "generic-secret-value")
    monkeypatch.setenv("ATHENA_CLIENT_ID", "medauth-athena-client")
    monkeypatch.setenv("ATHENA_CLIENT_SECRET", "athena-secret-value")
    # Every other vendor is left unregistered on purpose, so a test can assert
    # what an unconfigured vendor does without inventing an environment for it.
    for prefix in ("ECW", "MODMED", "CERNER", "EPIC"):
        monkeypatch.delenv(f"{prefix}_CLIENT_ID", raising=False)
        monkeypatch.delenv(f"{prefix}_CLIENT_SECRET", raising=False)
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def ehr() -> FakeAuthorizationServer:
    return FakeAuthorizationServer()


@pytest.fixture
def client(
    smart_env: None,
    fake_redis: FakeRedis,
    ehr: FakeAuthorizationServer,
) -> Iterator[TestClient]:
    """A test client with Redis and the EHR replaced.

    ``follow_redirects`` is off: the authorization redirect is the thing under
    test on the launch route, and following it would send the test at a URL
    nothing serves.
    """
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    http = ehr.client()
    app.dependency_overrides[get_http_client] = lambda: http
    with TestClient(app) as test_client:
        test_client.follow_redirects = False
        yield test_client

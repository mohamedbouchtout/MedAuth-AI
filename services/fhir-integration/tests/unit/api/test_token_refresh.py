"""EHR access token renewal. TASK-051b.

Driven through ``GET /fhir/encounter/{id}`` rather than against the renewal
function directly, because "transparently" is the whole claim: a caller that
knows nothing about tokens makes an ordinary read, and the read succeeds across
an expiry it never hears about.

The two halves of a failed renewal are what most of this file is about. An
authorization server that *refuses* the grant has decided something — the launch
is over. One that cannot be *reached* has decided nothing, and ending a working
launch on that would be reading silence as a negative determination, which is
the error CLAUDE.md rejects in the CRD path and again here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import status
from fastapi.testclient import TestClient

from src.adapters.factory import EHRType
from src.api import fhir as fhir_routes
from src.api.dependencies import get_http_client, get_redis
from src.main import create_app
from src.smart import store
from src.smart.store import LaunchToken
from tests.unit.api.conftest import TOKEN_ENDPOINT, FakeRedis
from tests.unit.conftest import FHIR_BASE_URL, FakeFHIRServer

LAUNCH_ID = "7c1d9e42-5b30-4a86-91ff-2e6a0c4b8d19"
HEADERS = {fhir_routes.LAUNCH_ID_HEADER: LAUNCH_ID}
STALE_TOKEN = "stale-access-token"
RENEWED_TOKEN = "renewed-access-token"
OLD_REFRESH = "refresh-token-one"
ROTATED_REFRESH = "refresh-token-two"


class FakeTokenEndpoint:
    """An authorization server that only answers refreshes.

    Attributes:
        requests: Every form posted, so a test can assert which grant was
            presented — that is how rotation is checked.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []
        self.status_code = 200
        self.body: dict[str, Any] = {
            "access_token": RENEWED_TOKEN,
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        #: Raised instead of answering, for the unreachable cases.
        self.raises: Exception | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in _parse_form(request).items()}
        self.requests.append(form)
        if self.raises is not None:
            raise self.raises
        return httpx.Response(self.status_code, json=self.body)


def _parse_form(request: httpx.Request) -> dict[str, list[str]]:
    from urllib.parse import parse_qs

    return parse_qs(request.content.decode())


def stored_token(
    *,
    expires_in: int,
    refresh_token: str | None = OLD_REFRESH,
    access_token: str = STALE_TOKEN,
) -> LaunchToken:
    """A launch record whose access token expires ``expires_in`` seconds from now."""
    return LaunchToken(
        ehr_type=EHRType.GENERIC,
        fhir_base_url=FHIR_BASE_URL,
        access_token=access_token,
        access_token_expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        token_endpoint=TOKEN_ENDPOINT,
        refresh_token=refresh_token,
        patient_id="synthea-123",
        encounter_id="encounter-1",
    )


class AuditRecorder:
    """Records what ``audit_log`` was called with, instead of writing a row."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> AuditRecorder:
    """Replace the audit write for every test here.

    Autouse because renewal happens on the way to a PHI read, so a test that
    only cares about the token still reaches the audit call. Patching our own
    function and asserting our own call to it is not the AWS-mocking shortcut
    CLAUDE.md warns about — nothing third-party is being stubbed and then
    asserted as if it had answered.
    """
    recorder = AuditRecorder()
    monkeypatch.setattr("src.audit.audit_log", recorder)
    return recorder


@pytest.fixture
def token_endpoint() -> FakeTokenEndpoint:
    return FakeTokenEndpoint()


@pytest.fixture
def ehr_server() -> FakeFHIRServer:
    return FakeFHIRServer()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def client(
    fake_redis: FakeRedis,
    ehr_server: FakeFHIRServer,
    token_endpoint: FakeTokenEndpoint,
    smart_env: None,
) -> Iterator[TestClient]:
    """A client whose one HTTP transport answers both the EHR and its token endpoint."""

    def route(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_ENDPOINT:
            return token_endpoint.handle(request)
        return ehr_server.handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(route))
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    app.dependency_overrides[get_http_client] = lambda: http
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def put(fake_redis: FakeRedis, token: LaunchToken) -> None:
    fake_redis.values[store.token_key(LAUNCH_ID)] = token.model_dump_json()


def read_record(fake_redis: FakeRedis) -> LaunchToken:
    return LaunchToken.model_validate_json(fake_redis.values[store.token_key(LAUNCH_ID)])


def reject(token_endpoint: FakeTokenEndpoint, *, error: str = "invalid_grant") -> None:
    """Make the authorization server refuse the grant, as a revocation would."""
    token_endpoint.status_code = 400
    token_endpoint.body = {"error": error}


class TestTransparentRenewal:
    """The acceptance case: a read succeeds across an expiry the caller never sees."""

    def test_an_expired_token_is_renewed_and_the_launch_id_is_unchanged(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
    ) -> None:
        put(fake_redis, stored_token(expires_in=-60))

        response = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert response.status_code == status.HTTP_200_OK
        assert token_endpoint.requests[0]["grant_type"] == "refresh_token"
        # The launch id names the launch, not the token. A client holding it is
        # never made to re-learn it.
        assert store.token_key(LAUNCH_ID) in fake_redis.values
        assert read_record(fake_redis).access_token == RENEWED_TOKEN

    def test_the_renewed_token_is_what_reaches_the_ehr(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        ehr_server: FakeFHIRServer,
    ) -> None:
        """Renewal that did not change the credential in use would prove nothing."""
        put(fake_redis, stored_token(expires_in=-60))

        client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert ehr_server.authorization_headers
        assert all(
            header == f"Bearer {RENEWED_TOKEN}" for header in ehr_server.authorization_headers
        )

    def test_a_token_inside_its_lifetime_triggers_no_refresh_at_all(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
    ) -> None:
        """Renewing on every read would be a round trip per call to no purpose."""
        put(fake_redis, stored_token(expires_in=3600))

        response = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert response.status_code == status.HTTP_200_OK
        assert token_endpoint.requests == []

    def test_a_token_inside_the_skew_margin_is_renewed_early(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
    ) -> None:
        """The EHR's clock is not ours, and the call is yet to be made.

        A token with seconds left is expired by the time it is presented, so the
        margin renews ahead of the deadline rather than exactly on it.
        """
        put(fake_redis, stored_token(expires_in=30))

        client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert len(token_endpoint.requests) == 1

    def test_renewal_writes_no_audit_row_of_its_own(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        audit: AuditRecorder,
    ) -> None:
        """Obtaining a credential is not using it — the same test TASK-051 applied.

        The read this renewal is on the way to still audits, exactly once, and
        that one row is the whole trail: a second row for the renewal would put
        an operational event in a table whose value is that every row is a PHI
        access.
        """
        put(fake_redis, stored_token(expires_in=-60))

        client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert len(audit.calls) == 1
        assert audit.calls[0]["action"].value == "READ_ENCOUNTER"


class TestRotation:
    """A new refresh token invalidates the one just presented."""

    def test_a_rotated_refresh_token_is_stored_and_used_next_time(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
    ) -> None:
        """Missing this breaks the *second* renewal while the first looks fine."""
        token_endpoint.body = {
            "access_token": RENEWED_TOKEN,
            "expires_in": 1,
            "refresh_token": ROTATED_REFRESH,
        }
        put(fake_redis, stored_token(expires_in=-60))

        client.get("/fhir/encounter/encounter-1", headers=HEADERS)
        assert read_record(fake_redis).refresh_token == ROTATED_REFRESH

        # The second renewal must present the rotated value, not the original.
        client.get("/fhir/encounter/encounter-1", headers=HEADERS)
        assert [request["refresh_token"] for request in token_endpoint.requests] == [
            OLD_REFRESH,
            ROTATED_REFRESH,
        ]

    def test_a_server_that_returns_no_new_grant_keeps_the_existing_one(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
    ) -> None:
        """Rotation is optional; dropping the grant when none is sent would end the launch."""
        put(fake_redis, stored_token(expires_in=-60))

        client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert read_record(fake_redis).refresh_token == OLD_REFRESH


class TestAFailedRenewalHasTwoOutcomes:
    """A refused grant ends the launch; an unreachable server does not."""

    def test_a_rejected_refresh_token_says_the_launch_must_be_repeated(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
    ) -> None:
        reject(token_endpoint)
        put(fake_redis, stored_token(expires_in=-60))

        response = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        error = response.json()["error"]
        assert error["code"] == "FHIR_LAUNCH_EXPIRED"
        assert "repeated" in error["message"]

    def test_a_refused_grant_is_not_presented_again(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
    ) -> None:
        """The record keeps answering 401, without re-asking a vendor that refused."""
        reject(token_endpoint)
        put(fake_redis, stored_token(expires_in=-60))

        first = client.get("/fhir/encounter/encounter-1", headers=HEADERS)
        second = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert first.status_code == second.status_code == status.HTTP_401_UNAUTHORIZED
        assert second.json()["error"]["code"] == "FHIR_LAUNCH_EXPIRED"
        # One attempt, not one per request.
        assert len(token_endpoint.requests) == 1
        assert read_record(fake_redis).refresh_token is None

    @pytest.mark.parametrize(
        ("failure", "expected_status"),
        [
            (httpx.ConnectError("refused"), status.HTTP_502_BAD_GATEWAY),
            (httpx.ReadTimeout("timed out"), status.HTTP_504_GATEWAY_TIMEOUT),
        ],
        ids=["connect-error", "timeout"],
    )
    def test_an_unreachable_token_endpoint_keeps_its_own_status_and_leaves_the_launch(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
        failure: Exception,
        expected_status: int,
    ) -> None:
        """We do not know the grant's fate, so nothing is written and it stays usable."""
        token_endpoint.raises = failure
        put(fake_redis, stored_token(expires_in=-60))

        response = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert response.status_code == expected_status
        assert response.json()["error"]["code"] == "FHIR_TOKEN_REFRESH_UNAVAILABLE"
        # The grant survives — this outcome must not end a working launch.
        assert read_record(fake_redis).refresh_token == OLD_REFRESH

    def test_a_server_error_is_unreachable_rather_than_a_refusal(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
    ) -> None:
        """A 500 from an authorization server is its failure, not its decision."""
        token_endpoint.status_code = 503
        token_endpoint.body = {"error": "server_error"}
        put(fake_redis, stored_token(expires_in=-60))

        response = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json()["error"]["code"] == "FHIR_TOKEN_REFRESH_UNAVAILABLE"
        assert read_record(fake_redis).refresh_token == OLD_REFRESH

    def test_an_unusable_answer_keeps_the_grant_rather_than_ending_the_launch(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
    ) -> None:
        """A 200 carrying no access token is not the server refusing the grant.

        It is the third outcome — an answer arrived and was unusable — and it is
        deliberately grouped with "unreachable" rather than with "refused",
        because nothing here says the refresh token is dead and discarding it
        would end a launch on a vendor's malformed response.
        """
        token_endpoint.body = {"token_type": "Bearer", "expires_in": 3600}
        put(fake_redis, stored_token(expires_in=-60))

        response = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json()["error"]["code"] == "FHIR_TOKEN_REFRESH_UNAVAILABLE"
        assert read_record(fake_redis).refresh_token == OLD_REFRESH

    def test_a_refusal_with_no_oauth_error_code_is_still_a_refusal(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
    ) -> None:
        """The code sharpens the log line; the 4xx is what decides the outcome.

        A server that refuses with an empty or non-JSON body has still refused,
        and reading the absent code as "not a refusal" would leave the launch
        presenting a grant the vendor will never honour.
        """
        token_endpoint.status_code = 400
        token_endpoint.body = {}
        put(fake_redis, stored_token(expires_in=-60))

        response = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json()["error"]["code"] == "FHIR_LAUNCH_EXPIRED"
        assert read_record(fake_redis).refresh_token is None


class TestARecordThisServiceCannotRead:
    """A record written by an older process, whose shape changed in TASK-051b."""

    def test_it_is_a_404_rather_than_a_500(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
    ) -> None:
        """ "No such launch" is the truthful answer for a record we cannot read.

        The pre-TASK-051b shape carried no ``token_endpoint`` and no
        ``access_token_expires_at``, so a record still in Redis across the
        upgrade would otherwise surface as an unhandled validation error.
        """
        fake_redis.values[store.token_key(LAUNCH_ID)] = (
            '{"ehr_type": "generic", "fhir_base_url": "https://fhir.example-hospital.org/r4", '
            '"access_token": "old-shape-token"}'
        )

        response = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["code"] == "FHIR_UNKNOWN_LAUNCH"


class TestALaunchWithNothingToRenew:
    """An EHR that issued no refresh token."""

    def test_an_expired_token_with_no_grant_is_the_launch_expiring(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
    ) -> None:
        put(fake_redis, stored_token(expires_in=-60, refresh_token=None))

        response = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json()["error"]["code"] == "FHIR_LAUNCH_EXPIRED"
        # Nothing was asked of the vendor, because nothing could be presented.
        assert token_endpoint.requests == []


class TestNoCredentialIsLogged:
    """The rule TASK-050 set for an access token, applied to the refresh token."""

    def test_no_token_reaches_a_log_record(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(0)
        put(fake_redis, stored_token(expires_in=-60))

        client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        logged = "\n".join(record.getMessage() for record in caplog.records)
        for secret in (STALE_TOKEN, RENEWED_TOKEN, OLD_REFRESH, LAUNCH_ID, "generic-secret"):
            assert secret not in logged

    def test_a_refused_grant_logs_the_oauth_code_and_no_credential(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
        token_endpoint: FakeTokenEndpoint,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``invalid_grant`` is the one part of the body worth surfacing."""
        caplog.set_level(0)
        token_endpoint.status_code = 400
        token_endpoint.body = {
            "error": "invalid_grant",
            "error_description": f"token {OLD_REFRESH} was revoked",
        }
        put(fake_redis, stored_token(expires_in=-60))

        client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "invalid_grant" in logged
        # error_description is free text an authorization server may fill with
        # an echo of the request, which is why only the code is read.
        assert OLD_REFRESH not in logged

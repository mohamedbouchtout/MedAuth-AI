"""The SMART on FHIR launch flow, end to end against a fake authorization server.

Covers TASK-051's acceptance criteria: the full flow, the state mismatch that is
this flow's CSRF defence, PKCE, `aud`, single-use state, a discovery document
that is not there, and the rule that no credential reaches a log line.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from src.adapters.factory import EHRType
from src.smart.pkce import derive_code_challenge
from src.smart.store import LaunchToken, PendingLaunch, launch_key, token_key
from tests.unit import idtokens

from .conftest import (
    ATHENA_ISS,
    AUTHORIZATION_ENDPOINT,
    GENERIC_ISS,
    REDIRECT_URI,
    TOKEN_ENDPOINT,
    FakeAuthorizationServer,
    FakeRedis,
)


def start_launch(client: TestClient, *, iss: str = GENERIC_ISS, launch: str | None = None) -> str:
    """Drive the launch route and return the authorization URL it redirected to."""
    params: dict[str, str] = {"iss": iss}
    if launch is not None:
        params["launch"] = launch

    response = client.get("/fhir/launch", params=params)
    assert response.status_code == status.HTTP_302_FOUND, response.text
    return response.headers["location"]


def state_from(redirect_url: str) -> str:
    """Pull the state out of an authorization redirect."""
    return parse_qs(urlsplit(redirect_url).query)["state"][0]


class TestFullFlow:
    """A launch that completes, which is TASK-051's first acceptance test."""

    def test_ehr_launch_produces_a_launch_id_and_a_stored_token(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> None:
        redirect_url = start_launch(client, launch="opaque-ehr-context")
        params = ehr.observe_authorization(redirect_url)

        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["error"] is None
        assert body["data"]["ehr_type"] == EHRType.GENERIC
        assert body["data"]["expires_in"] == 3600

        launch_id = body["data"]["launch_id"]
        stored = LaunchToken.model_validate_json(fake_redis.values[token_key(launch_id)])
        assert stored.access_token == "ehr-access-token"
        assert stored.fhir_base_url == GENERIC_ISS
        assert stored.ehr_type == EHRType.GENERIC

    def test_the_record_outlives_the_access_token_when_a_grant_can_renew_it(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> None:
        """TASK-051b, reversing TASK-051 — and this is the regression test for it.

        This test previously asserted the opposite (``== 3600``, the access
        token's own lifetime), under the rule that a record must not outlive the
        credential it holds. That rule deleted the only copy of the refresh
        token at the exact moment renewal needed it, which made renewal
        impossible rather than merely unbuilt. The assertion is inverted
        deliberately, not relaxed to accommodate new code: against the shipped
        contract this fails. See CLAUDE.md, "The launch record outlives its
        access token".
        """
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )

        launch_id = response.json()["data"]["launch_id"]
        # The refresh grant's bound, not the access token's 3600.
        assert fake_redis.expiries[token_key(launch_id)] == 28800

        # And the access token's own expiry survives as a field, which is what
        # renewal reads. One TTL cannot carry two lifetimes.
        stored = LaunchToken.model_validate_json(fake_redis.values[token_key(launch_id)])
        remaining = (stored.access_token_expires_at - datetime.now(UTC)).total_seconds()
        assert 3590 < remaining <= 3600

    def test_a_launch_with_no_refresh_token_still_expires_with_the_token(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> None:
        """What the reversed rule keeps rather than discards.

        With nothing to renew, holding a patient identifier beside a dead
        credential for eight hours buys nothing — so the original behaviour is
        still right for this case, and is preserved on purpose.
        """
        ehr.token_body = {"access_token": "no-refresh-token", "expires_in": 900}

        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )

        launch_id = response.json()["data"]["launch_id"]
        assert fake_redis.expiries[token_key(launch_id)] == 900

    def test_a_token_response_without_expires_in_gets_a_short_floor(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> None:
        """SMART makes expires_in optional; a record with no TTL would never expire.

        This one is unchanged by TASK-051b's reversal, because the fake's body
        here carries no refresh token either — the floor still bounds both the
        stored expiry and, with nothing to renew, the record itself.
        """
        ehr.token_body = {"access_token": "no-expiry-token"}

        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )

        launch_id = response.json()["data"]["launch_id"]
        assert fake_redis.expiries[token_key(launch_id)] == 300

    def test_the_token_endpoint_is_carried_onto_the_launch_record(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> None:
        """Renewal must not have to rediscover it. TASK-051b.

        ``PendingLaunch`` is consumed by the callback, so without this the
        endpoint is gone by the time a refresh needs it — and rediscovering
        would let two halves of one OAuth conversation come from two documents.
        """
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )

        launch_id = response.json()["data"]["launch_id"]
        stored = LaunchToken.model_validate_json(fake_redis.values[token_key(launch_id)])
        assert stored.token_endpoint == TOKEN_ENDPOINT

    def test_refresh_token_and_launch_context_are_stored_for_later_tasks(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> None:
        """TASK-051b refreshes and TASK-052 reads the context; neither re-exchanges."""
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )

        launch_id = response.json()["data"]["launch_id"]
        stored = LaunchToken.model_validate_json(fake_redis.values[token_key(launch_id)])
        assert stored.refresh_token == "ehr-refresh-token"
        assert stored.patient_id == "Patient/synthea-123"

    def test_the_callback_does_not_return_the_launch_context(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
    ) -> None:
        """A credential exchange is not where a client starts learning patient ids."""
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )

        assert "synthea-123" not in response.text
        assert set(response.json()["data"]) == {"launch_id", "ehr_type", "expires_in"}


class TestStateIsTheCsrfDefence:
    """TASK-051's second acceptance test, and what makes a state single-use."""

    def test_state_mismatch_returns_400(self, client: TestClient) -> None:
        start_launch(client, launch="ctx")

        response = client.get(
            "/fhir/callback", params={"state": "a-state-nobody-issued", "code": "auth-code"}
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["error"]["code"] == "SMART_UNKNOWN_STATE"

    def test_replaying_a_consumed_state_returns_400(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> None:
        """The second callback must not mint a second token for one launch."""
        redirect_url = start_launch(client, launch="ctx")
        state = state_from(redirect_url)
        ehr.observe_authorization(redirect_url)

        first = client.get("/fhir/callback", params={"state": state, "code": "auth-code"})
        assert first.status_code == status.HTTP_200_OK

        replay = client.get("/fhir/callback", params={"state": state, "code": "auth-code"})

        assert replay.status_code == status.HTTP_400_BAD_REQUEST
        assert replay.json()["error"]["code"] == "SMART_UNKNOWN_STATE"
        assert len(ehr.token_requests) == 1, "the replay reached the authorization server"

    def test_the_launch_record_is_deleted_once_consumed(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> None:
        """Leaving it readable keeps a replayable CSRF token alive for its whole TTL."""
        redirect_url = start_launch(client, launch="ctx")
        state = state_from(redirect_url)
        ehr.observe_authorization(redirect_url)

        client.get("/fhir/callback", params={"state": state, "code": "auth-code"})

        assert launch_key(state) not in fake_redis.values

    def test_a_state_is_not_taken_from_the_caller(self, client: TestClient) -> None:
        """A caller-chosen state would defend against nothing."""
        first = state_from(start_launch(client, launch="ctx"))
        second = state_from(start_launch(client, launch="ctx"))

        assert first != second

    def test_the_pending_launch_expires_with_the_configured_ttl(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
    ) -> None:
        state = state_from(start_launch(client, launch="ctx"))

        assert fake_redis.expiries[launch_key(state)] == 600


class TestPkce:
    """SMART on FHIR 2.0 requires PKCE of every client, confidential ones included."""

    def test_the_challenge_is_the_s256_hash_of_the_verifier_that_is_sent_later(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> None:
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        state = params["state"]

        pending = PendingLaunch.model_validate_json(fake_redis.values[launch_key(state)])

        assert params["code_challenge_method"] == "S256"
        assert params["code_challenge"] == derive_code_challenge(pending.code_verifier)

        client.get("/fhir/callback", params={"state": state, "code": "auth-code"})
        assert ehr.token_requests[0]["code_verifier"] == pending.code_verifier

    def test_the_verifier_never_appears_in_the_redirect(
        self,
        client: TestClient,
        fake_redis: FakeRedis,
    ) -> None:
        """The whole point: only the challenge travels where an attacker can see it."""
        redirect_url = start_launch(client, launch="ctx")
        state = state_from(redirect_url)
        pending = PendingLaunch.model_validate_json(fake_redis.values[launch_key(state)])

        assert pending.code_verifier not in redirect_url

    def test_a_mismatched_verifier_is_refused_by_the_authorization_server(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> None:
        """Proves the fake actually checks PKCE, so the tests above mean something."""
        redirect_url = start_launch(client, launch="ctx")
        state = state_from(redirect_url)
        ehr.observe_authorization(redirect_url)

        # Rewrite the stored verifier so the one presented no longer matches.
        pending = PendingLaunch.model_validate_json(fake_redis.values[launch_key(state)])
        tampered = pending.model_copy(update={"code_verifier": "a" * 43})
        fake_redis.values[launch_key(state)] = tampered.model_dump_json()

        response = client.get("/fhir/callback", params={"state": state, "code": "auth-code"})

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json()["error"]["code"] == "SMART_TOKEN_EXCHANGE_FAILED"
        assert "invalid_grant" in response.json()["error"]["message"]


class TestAuthorizationRequestParameters:
    """`aud` and the launch-type scopes, both required by the standard."""

    def test_aud_is_the_fhir_base_url(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        params = ehr.observe_authorization(start_launch(client, iss=ATHENA_ISS, launch="ctx"))

        assert params["aud"] == ATHENA_ISS

    def test_an_ehr_launch_asks_for_the_launch_scope_and_passes_the_context(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        params = ehr.observe_authorization(start_launch(client, launch="opaque-ehr-context"))

        assert params["launch"] == "opaque-ehr-context"
        assert params["scope"].split()[-1] == "launch"

    def test_a_standalone_launch_asks_for_launch_patient_and_passes_no_context(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        params = ehr.observe_authorization(start_launch(client))

        assert "launch" not in params
        assert params["scope"].split()[-1] == "launch/patient"

    def test_the_redirect_uri_is_the_configured_one(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        """A mismatch here is refused by the vendor, invisibly from our side."""
        params = ehr.observe_authorization(start_launch(client, launch="ctx"))

        assert params["redirect_uri"] == REDIRECT_URI

    def test_the_redirect_goes_to_the_discovered_authorization_endpoint(
        self, client: TestClient
    ) -> None:
        redirect_url = start_launch(client, launch="ctx")

        assert redirect_url.startswith(AUTHORIZATION_ENDPOINT)

    def test_the_same_redirect_uri_is_sent_on_the_exchange(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        """OAuth requires the two to agree; a silent divergence fails at the vendor."""
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        client.get("/fhir/callback", params={"state": params["state"], "code": "auth-code"})

        assert ehr.token_requests[0]["redirect_uri"] == REDIRECT_URI


class TestVendorSelection:
    """The flow keys off EHRType and introduces no second vendor identifier."""

    def test_a_vendor_issuer_uses_that_vendors_client_id(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        params = ehr.observe_authorization(start_launch(client, iss=ATHENA_ISS, launch="ctx"))

        assert params["client_id"] == "medauth-athena-client"

    def test_an_unrecognised_issuer_uses_the_generic_registration(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        """TASK-050 routes an unknown issuer to the standard adapter; it still launches."""
        params = ehr.observe_authorization(start_launch(client, launch="ctx"))

        assert params["client_id"] == "medauth-generic-client"

    def test_an_unregistered_vendor_fails_with_a_named_error(self, client: TestClient) -> None:
        """Epic resolves, but this deployment has no Epic registration."""
        response = client.get(
            "/fhir/launch", params={"iss": "https://fhir.epic.com/interconnect-fhir-oauth"}
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert response.json()["error"]["code"] == "SMART_CLIENT_NOT_REGISTERED"

    def test_a_confidential_client_authenticates_with_basic(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        client.get("/fhir/callback", params={"state": params["state"], "code": "auth-code"})

        assert ehr.token_auth_headers[0] is not None
        assert ehr.token_auth_headers[0].startswith("Basic ")

    def test_a_public_client_sends_no_basic_auth(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A registration with no secret is a public client, which PKCE alone covers."""
        from src.config import get_settings

        get_settings.cache_clear()
        monkeypatch.delenv("GENERIC_CLIENT_SECRET", raising=False)

        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        client.get("/fhir/callback", params={"state": params["state"], "code": "auth-code"})

        assert ehr.token_auth_headers[0] is None
        assert ehr.token_requests[0]["client_id"] == "medauth-generic-client"


class TestUpstreamFailures:
    """What a vendor sandbox that cannot be launched against actually says."""

    def test_a_missing_discovery_document_names_it_and_the_host(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        """TASK-051's scope boundary: no CapabilityStatement fallback, but a clear no."""
        ehr.discovery_status = 404

        response = client.get("/fhir/launch", params={"iss": GENERIC_ISS})

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        message = response.json()["error"]["message"]
        assert response.json()["error"]["code"] == "SMART_DISCOVERY_FAILED"
        assert ".well-known/smart-configuration" in message
        assert "fhir.example-hospital.org" in message
        assert "CapabilityStatement" in message

    def test_a_discovery_document_missing_an_endpoint_is_not_a_key_error(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        ehr.discovery_body = {"authorization_endpoint": AUTHORIZATION_ENDPOINT}

        response = client.get("/fhir/launch", params={"iss": GENERIC_ISS})

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert "token_endpoint" in response.json()["error"]["message"]

    def test_a_non_json_discovery_document_is_reported_as_such(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        ehr.discovery_body = "<html>login page</html>"

        response = client.get("/fhir/launch", params={"iss": GENERIC_ISS})

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert "not JSON" in response.json()["error"]["message"]

    def test_a_refused_token_exchange_surfaces_the_oauth_error_code(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        """invalid_client and invalid_grant mean very different things when debugging."""
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        ehr.token_status = 401
        ehr.token_body = {"error": "invalid_client", "error_description": "bad secret"}

        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert "invalid_client" in response.json()["error"]["message"]

    def test_an_error_description_is_not_echoed_back(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        """Free text an authorization server may fill with an echo of the request."""
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)
        ehr.token_status = 400
        ehr.token_body = {"error": "invalid_grant", "error_description": "code was abc123"}

        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )

        assert "abc123" not in response.text

    def test_a_declined_authorization_is_a_400_not_a_502(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        """The provider said no; nothing upstream is broken."""
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)

        response = client.get(
            "/fhir/callback", params={"state": params["state"], "error": "access_denied"}
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["error"]["code"] == "SMART_AUTHORIZATION_DENIED"

    def test_a_callback_with_neither_code_nor_error_is_a_400(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        redirect_url = start_launch(client, launch="ctx")
        params = ehr.observe_authorization(redirect_url)

        response = client.get("/fhir/callback", params={"state": params["state"]})

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_a_launch_without_iss_is_rejected(self, client: TestClient) -> None:
        response = client.get("/fhir/launch")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["error"] is not None


class TestNoCredentialIsLogged:
    """CLAUDE.md's rule, asserted rather than trusted to review.

    Assertions run against this service's own log records — the ``src.*``
    loggers — and not against every record the test process emits. Two other
    things log here and neither is this service's behaviour: the ``TestClient``
    logs the requests the *test* makes, which in a real deployment is the
    browser's side of a redirect and not ours; and ``httpx`` logs the requests
    this service makes outward, which is a library's own INFO output.

    That second one is not waved away — it is the reason ``iss`` is normalised
    before it reaches the client at all, which
    ``test_the_issuer_is_logged_as_a_host_not_a_url`` covers directly by
    asserting on the httpx records too.
    """

    @staticmethod
    def _our_records(caplog: pytest.LogCaptureFixture) -> str:
        return "\n".join(
            record.getMessage() for record in caplog.records if record.name.startswith("src.")
        )

    #: Everything that passes through these routes and must not be logged. The
    #: iss is here as a full URL because only its host may be logged.
    SECRETS = (
        "opaque-ehr-context",
        "auth-code",
        "ehr-access-token",
        "ehr-refresh-token",
        "generic-secret-value",
        "Patient/synthea-123",
    )

    def test_a_whole_launch_logs_no_credential(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            redirect_url = start_launch(client, launch="opaque-ehr-context")
            state = state_from(redirect_url)
            ehr.observe_authorization(redirect_url)
            pending = PendingLaunch.model_validate_json(fake_redis.values[launch_key(state)])
            client.get("/fhir/callback", params={"state": state, "code": "auth-code"})

        logged = self._our_records(caplog)
        for secret in self.SECRETS:
            assert secret not in logged, f"{secret!r} reached a log line"
        assert state not in logged, "the state reached a log line"
        assert pending.code_verifier not in logged, "the code verifier reached a log line"

    def test_the_issuer_is_logged_as_a_host_not_a_url(
        self,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A launch URL can carry context in its query string.

        This one deliberately asserts over more than the ``src.*`` loggers: the
        URL this service requests outward is built from ``iss``, and the HTTP
        client logs its own requests, so normalising ``iss`` at the edge is the
        only thing that keeps a stray query parameter out of that record. No log
        statement in this service could have fixed it.

        Records naming ``testserver`` are excluded, and only those: that is the
        ``TestClient`` logging the request the test itself made, which stands in
        for the browser's leg of the redirect rather than for anything this
        service does.
        """
        iss_with_context = f"{GENERIC_ISS}?practice=confidential-value"

        with caplog.at_level(logging.INFO):
            response = client.get("/fhir/launch", params={"iss": iss_with_context, "launch": "ctx"})

        assert response.status_code == status.HTTP_302_FOUND, (
            "a stray query parameter must not break discovery"
        )
        outward = "\n".join(
            message
            for record in caplog.records
            if "testserver" not in (message := record.getMessage())
        )
        assert "confidential-value" not in outward
        assert "fhir.example-hospital.org" in self._our_records(caplog)

    def test_an_unregistered_vendor_logs_the_variable_not_a_secret(
        self,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.ERROR):
            client.get("/fhir/launch", params={"iss": "https://fhir.epic.com/oauth"})

        assert "EPIC_CLIENT_ID" in self._our_records(caplog)


class TestTheLaunchActor:
    """TASK-051c: who authorized this launch, from a verified id_token.

    The launch never fails over this. Every case below either records a
    verified Practitioner or records nothing, and all of them return 200.
    """

    def complete(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
    ) -> LaunchToken:
        """Run a whole launch and return the record it stored."""
        redirect_url = start_launch(client, launch="opaque-ehr-context")
        params = ehr.observe_authorization(redirect_url)
        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        launch_id = response.json()["data"]["launch_id"]
        return LaunchToken.model_validate_json(fake_redis.values[token_key(launch_id)])

    def test_a_verifiable_fhir_user_is_stored_as_the_actor(
        self, client: TestClient, ehr: FakeAuthorizationServer, fake_redis: FakeRedis
    ) -> None:
        stored = self.complete(client, ehr, fake_redis)

        assert stored.fhir_practitioner_ref == f"{GENERIC_ISS}/Practitioner/prov-77"

    def test_an_id_token_that_does_not_verify_leaves_the_actor_null(
        self, client: TestClient, ehr: FakeAuthorizationServer, fake_redis: FakeRedis
    ) -> None:
        """Signed by a key the EHR does not publish. The launch still works."""
        ehr.token_body = {
            "access_token": "ehr-access-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "ehr-refresh-token",
            "id_token": idtokens.id_token(tag="secondary"),
        }

        stored = self.complete(client, ehr, fake_redis)

        assert stored.fhir_practitioner_ref is None
        assert stored.access_token == "ehr-access-token"

    def test_no_id_token_at_all_leaves_the_actor_null(
        self, client: TestClient, ehr: FakeAuthorizationServer, fake_redis: FakeRedis
    ) -> None:
        ehr.token_body = {
            "access_token": "ehr-access-token",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

        stored = self.complete(client, ehr, fake_redis)

        assert stored.fhir_practitioner_ref is None

    def test_an_ehr_that_publishes_no_keys_leaves_the_actor_null(
        self, client: TestClient, ehr: FakeAuthorizationServer, fake_redis: FakeRedis
    ) -> None:
        """SMART marks issuer and jwks_uri conditional — this server is conformant."""
        ehr.publishes_sso = False

        stored = self.complete(client, ehr, fake_redis)

        assert stored.fhir_practitioner_ref is None

    def test_a_token_minted_for_another_client_leaves_the_actor_null(
        self, client: TestClient, ehr: FakeAuthorizationServer, fake_redis: FakeRedis
    ) -> None:
        ehr.token_body = {
            "access_token": "ehr-access-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "id_token": idtokens.id_token(audience="a-different-app"),
        }

        stored = self.complete(client, ehr, fake_redis)

        assert stored.fhir_practitioner_ref is None

    def test_the_callback_response_carries_no_actor(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        """A credential exchange hands back a launch_id and nothing else.

        The actor is stored for the routes that audit, not returned: the same
        reason the SMART launch context is withheld here.
        """
        redirect_url = start_launch(client, launch="opaque-ehr-context")
        params = ehr.observe_authorization(redirect_url)

        response = client.get(
            "/fhir/callback", params={"state": params["state"], "code": "auth-code"}
        )

        assert set(response.json()["data"]) == {"launch_id", "ehr_type", "expires_in"}

    def test_neither_the_id_token_nor_the_actor_reaches_a_log_line(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        token = idtokens.id_token()
        ehr.token_body = {
            "access_token": "ehr-access-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "id_token": token,
        }

        stored = self.complete(client, ehr, fake_redis)

        assert stored.fhir_practitioner_ref is not None
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert token not in logged
        assert "prov-77" not in logged

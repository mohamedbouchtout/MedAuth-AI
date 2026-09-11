"""TASK-051g — handing a launch that did *not* complete back to its client.

The gap this closes: TASK-051f delivered a completed launch to the app that
started it and said nothing about one that fails, so `GET /fhir/callback` raised
identically whatever `delivery` the launch declared. A `delivery=web` launch the
provider declines rendered a JSON error document on this service's own origin —
the provider left on a page that is not MedAuth with no link back, and the app
that started the launch never learning the launch ended.

These tests hold what CLAUDE.md fixes in "A failed launch is delivered the same
way, and carries no claim code":

- a failed `web`/`mobile` launch **redirects to that platform's return target**
  carrying an error code and **no claim code**;
- a `json` launch, and a launch that declared nothing, **keep raising** — the
  service-to-service caller is supplemented, never replaced;
- **no `fhir_launch_claim:` record is written** for a launch that did not
  complete;
- an **unknown `state` raises for every delivery**, because the record that would
  have declared one is gone;
- the **EHR's own error string reaches neither the redirect nor a log line**.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from src.smart.delivery import LaunchDelivery, LaunchFailure
from tests.conftest import TEST_MOBILE_RETURN_URI, TEST_WEB_RETURN_URL

from .conftest import GENERIC_ISS, FakeAuthorizationServer, FakeRedis

#: The two deliveries that reach a client, and the target each is configured
#: with. Every failure case runs against both, because a fix applied to one
#: platform's path only is exactly the divergence CLAUDE.md settles this
#: question centrally to prevent.
CLIENT_DELIVERIES = [
    pytest.param(LaunchDelivery.WEB, TEST_WEB_RETURN_URL, id="web"),
    pytest.param(LaunchDelivery.MOBILE, TEST_MOBILE_RETURN_URI, id="mobile"),
]

#: What a launch declares when no client is waiting: an EHR-initiated launch, or
#: a service-to-service caller. Both must keep raising.
JSON_DELIVERIES = [
    pytest.param("json", id="explicit-json"),
    pytest.param(None, id="absent"),
]


def start(client: TestClient, ehr: FakeAuthorizationServer, delivery: str | None) -> str:
    """Begin a launch and return the state the authorization server saw."""
    params = {"iss": GENERIC_ISS, "launch": "opaque-ehr-context"}
    if delivery is not None:
        params["delivery"] = delivery
    redirect_url = client.get("/fhir/launch", params=params).headers["location"]
    return ehr.observe_authorization(redirect_url)["state"]


def query_of(redirect_url: str) -> dict[str, list[str]]:
    """Parse a return redirect's query string."""
    return parse_qs(urlsplit(redirect_url).query)


def claim_keys(fake_redis: FakeRedis) -> list[str]:
    """Every handoff record currently held."""
    return [key for key in fake_redis.values if key.startswith("fhir_launch_claim:")]


class TestAFailedClientLaunchReachesTheClient:
    """The acceptance test TASK-051g names first."""

    @pytest.mark.parametrize(("delivery", "target"), CLIENT_DELIVERIES)
    def test_a_declined_launch_redirects_with_an_error_and_no_claim(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        delivery: LaunchDelivery,
        target: str,
    ) -> None:
        state = start(client, ehr, delivery.value)

        response = client.get("/fhir/callback", params={"state": state, "error": "access_denied"})

        assert response.status_code == status.HTTP_302_FOUND, response.text
        location = response.headers["location"]
        assert location.startswith(f"{target}?")
        query = query_of(location)
        assert query["error"] == [LaunchFailure.DECLINED.value]
        # No claim code, because there is no launch to name. Issuing one would
        # put a credential-shaped value in a redirect with nothing behind it.
        assert "claim" not in query

    @pytest.mark.parametrize(("delivery", "target"), CLIENT_DELIVERIES)
    def test_a_callback_carrying_neither_code_nor_error_is_failed_not_declined(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        delivery: LaunchDelivery,
        target: str,
    ) -> None:
        """Nobody refused anything — telling a provider they cancelled would lie."""
        state = start(client, ehr, delivery.value)

        response = client.get("/fhir/callback", params={"state": state})

        assert response.status_code == status.HTTP_302_FOUND, response.text
        assert query_of(response.headers["location"])["error"] == [LaunchFailure.FAILED.value]

    @pytest.mark.parametrize(("delivery", "target"), CLIENT_DELIVERIES)
    def test_a_refused_token_exchange_is_delivered_as_failed(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        delivery: LaunchDelivery,
        target: str,
    ) -> None:
        """The third deliverable failure, and the one raised from an except block."""
        state = start(client, ehr, delivery.value)
        ehr.token_status = 400
        ehr.token_body = {"error": "invalid_grant", "error_description": "code was abc123"}

        response = client.get("/fhir/callback", params={"state": state, "code": "auth-code"})

        assert response.status_code == status.HTTP_302_FOUND, response.text
        location = response.headers["location"]
        assert query_of(location)["error"] == [LaunchFailure.FAILED.value]
        assert "abc123" not in location

    @pytest.mark.parametrize(("delivery", "target"), CLIENT_DELIVERIES)
    def test_the_error_is_one_of_the_closed_vocabulary(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        delivery: LaunchDelivery,
        target: str,
    ) -> None:
        """A client narrows this value; a free-form string would defeat that."""
        state = start(client, ehr, delivery.value)

        response = client.get("/fhir/callback", params={"state": state, "error": "server_error"})

        delivered = query_of(response.headers["location"])["error"][0]
        assert delivered in {member.value for member in LaunchFailure}


class TestTheJsonCallerIsSupplementedNotReplaced:
    """The caller that already worked keeps working, unchanged."""

    @pytest.mark.parametrize("delivery", JSON_DELIVERIES)
    def test_a_declined_launch_still_raises(
        self, client: TestClient, ehr: FakeAuthorizationServer, delivery: str | None
    ) -> None:
        state = start(client, ehr, delivery)

        response = client.get("/fhir/callback", params={"state": state, "error": "access_denied"})

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
        assert response.json()["data"] is None
        assert response.json()["error"]["code"] == "SMART_AUTHORIZATION_DENIED"

    @pytest.mark.parametrize("delivery", JSON_DELIVERIES)
    def test_a_refused_token_exchange_still_raises(
        self, client: TestClient, ehr: FakeAuthorizationServer, delivery: str | None
    ) -> None:
        state = start(client, ehr, delivery)
        ehr.token_status = 401
        ehr.token_body = {"error": "invalid_client"}

        response = client.get("/fhir/callback", params={"state": state, "code": "auth-code"})

        assert response.status_code == status.HTTP_502_BAD_GATEWAY, response.text
        # The operational detail a service-to-service caller relies on is still
        # in the message it already got.
        assert "invalid_client" in response.json()["error"]["message"]


class TestAFailedLaunchWritesNoHandoffRecord:
    """There is no launch to name, so there is nothing to hold a code against."""

    @pytest.mark.parametrize(("delivery", "target"), CLIENT_DELIVERIES)
    def test_a_declined_launch_writes_no_claim(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
        delivery: LaunchDelivery,
        target: str,
    ) -> None:
        state = start(client, ehr, delivery.value)

        client.get("/fhir/callback", params={"state": state, "error": "access_denied"})

        assert claim_keys(fake_redis) == []

    @pytest.mark.parametrize(("delivery", "target"), CLIENT_DELIVERIES)
    def test_a_failed_token_exchange_writes_no_claim_and_no_token(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
        delivery: LaunchDelivery,
        target: str,
    ) -> None:
        state = start(client, ehr, delivery.value)
        ehr.token_status = 400
        ehr.token_body = {"error": "invalid_grant"}

        client.get("/fhir/callback", params={"state": state, "code": "auth-code"})

        assert claim_keys(fake_redis) == []
        assert [key for key in fake_redis.values if key.startswith("fhir_token:")] == []


class TestAnUnknownStateCannotBeDelivered:
    """The one failure with no record to declare a delivery — and no fallback.

    Stated as a test rather than only as a comment because the tempting "fix" is
    to read ``delivery`` off the callback's own request, which is the inference
    the success answer already refuses and would let anyone who can reach this
    route choose where the service redirects a browser.
    """

    @pytest.mark.parametrize(("delivery", "target"), CLIENT_DELIVERIES)
    def test_a_replayed_state_raises_even_for_a_client_delivery(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        delivery: LaunchDelivery,
        target: str,
    ) -> None:
        """The record is consumed by the first callback, so the second has none."""
        state = start(client, ehr, delivery.value)
        first = client.get("/fhir/callback", params={"state": state, "error": "access_denied"})

        second = client.get("/fhir/callback", params={"state": state, "error": "access_denied"})

        assert first.status_code == status.HTTP_302_FOUND
        assert second.status_code == status.HTTP_400_BAD_REQUEST, second.text
        assert second.json()["error"]["code"] == "SMART_UNKNOWN_STATE"

    def test_a_state_that_was_never_issued_raises(self, client: TestClient) -> None:
        response = client.get("/fhir/callback", params={"state": "never-issued", "code": "c"})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["error"]["code"] == "SMART_UNKNOWN_STATE"

    def test_a_delivery_parameter_on_the_callback_changes_nothing(self, client: TestClient) -> None:
        """No fallback that takes the declaration from the party being answered."""
        response = client.get(
            "/fhir/callback",
            params={"state": "never-issued", "error": "access_denied", "delivery": "web"},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "location" not in response.headers


class TestTheVendorsOwnReasonGoesNoFurtherThanTheLog:
    """An EHR's error string in our UI is a third party's text read as ours."""

    @pytest.mark.parametrize(("delivery", "target"), CLIENT_DELIVERIES)
    def test_the_oauth_error_code_is_not_in_the_redirect(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        delivery: LaunchDelivery,
        target: str,
    ) -> None:
        state = start(client, ehr, delivery.value)

        response = client.get(
            "/fhir/callback",
            params={"state": state, "error": "consent_required_for_patient_jane_doe"},
        )

        assert "jane_doe" not in response.headers["location"]

    @pytest.mark.parametrize(("delivery", "target"), CLIENT_DELIVERIES)
    def test_no_log_line_names_the_state(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        caplog: pytest.LogCaptureFixture,
        delivery: LaunchDelivery,
        target: str,
    ) -> None:
        """The delivery log names which client was told what, and nothing else.

        The ``state`` is a credential this flow already refuses to log, and the
        failure path must not become the place it starts appearing.
        """
        state = start(client, ehr, delivery.value)

        with caplog.at_level(logging.DEBUG):
            client.get("/fhir/callback", params={"state": state, "error": "access_denied"})

        emitted = "\n".join(record.getMessage() for record in caplog.records)
        assert state not in emitted
        assert delivery.value in emitted

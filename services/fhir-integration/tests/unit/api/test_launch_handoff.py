"""TASK-051f — handing a completed launch back to the app that started it.

The gap this closes: `GET /fhir/callback` answered with JSON in whatever browser
the EHR redirected, which a service-to-service caller reads and an application
cannot. These tests hold the four properties CLAUDE.md fixes in "Handing a
completed SMART launch back to a client":

- the JSON answer is **supplemented, not replaced** — the caller that already
  worked still works, and it is the launch's own declaration that decides;
- a claim code is **redeemable exactly once**, and the second attempt is a 404
  rather than a second working handle;
- **no `launch_id` reaches a log line or a redirect URL** — the whole reason the
  redirect carries a claim code and not the launch itself;
- a **malformed or missing return target refuses to boot**, rather than stranding
  a launch at the end of an OAuth redirect chain.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from src.adapters.factory import EHRType
from src.config import Settings, get_settings, validate_return_targets
from src.smart.delivery import (
    LaunchDelivery,
    ReturnTargetError,
    append_claim,
    redirect_target_for,
    validate_mobile_return_uri,
    validate_web_return_url,
)
from src.smart.store import PendingLaunch, launch_claim_key, launch_key
from tests.conftest import TEST_MOBILE_RETURN_URI, TEST_WEB_RETURN_URL

from .conftest import GENERIC_ISS, FakeAuthorizationServer, FakeRedis


def complete_launch(
    client: TestClient,
    ehr: FakeAuthorizationServer,
    *,
    delivery: str | None = None,
    launch: str | None = "opaque-ehr-context",
) -> tuple[str, object]:
    """Drive a whole launch and return the state used plus the callback's response."""
    params: dict[str, str] = {"iss": GENERIC_ISS}
    if launch is not None:
        params["launch"] = launch
    if delivery is not None:
        params["delivery"] = delivery

    redirect_url = client.get("/fhir/launch", params=params).headers["location"]
    observed = ehr.observe_authorization(redirect_url)
    response = client.get(
        "/fhir/callback", params={"state": observed["state"], "code": "auth-code"}
    )
    return observed["state"], response


def claim_from(redirect_url: str) -> str:
    """Pull the handoff code out of a client return redirect."""
    return parse_qs(urlsplit(redirect_url).query)["claim"][0]


class TestTheJsonAnswerIsSupplementedNotReplaced:
    """Two genuinely different callers, and the first one keeps working."""

    def test_a_launch_that_declared_nothing_still_answers_json(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        """An EHR-initiated launch has no client waiting, and this is unchanged."""
        _, response = complete_launch(client, ehr)

        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["error"] is None
        assert body["data"]["launch_id"]
        assert body["data"]["ehr_type"] == EHRType.GENERIC

    def test_an_explicit_json_delivery_answers_json(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        _, response = complete_launch(client, ehr, delivery="json")

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["data"]["launch_id"]

    @pytest.mark.parametrize(
        ("delivery", "expected_target"),
        [
            (LaunchDelivery.WEB, TEST_WEB_RETURN_URL),
            (LaunchDelivery.MOBILE, TEST_MOBILE_RETURN_URI),
        ],
    )
    def test_a_client_delivery_redirects_to_that_platforms_target(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        delivery: LaunchDelivery,
        expected_target: str,
    ) -> None:
        _, response = complete_launch(client, ehr, delivery=delivery.value)

        assert response.status_code == status.HTTP_302_FOUND, response.text
        location = response.headers["location"]
        assert location.startswith(f"{expected_target}?claim=")

    def test_an_unknown_delivery_is_refused_rather_than_defaulted(self, client: TestClient) -> None:
        """A closed vocabulary: a value nobody defined is a 422, not a silent json."""
        response = client.get(
            "/fhir/launch", params={"iss": GENERIC_ISS, "delivery": "carrier-pigeon"}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_the_decision_comes_off_the_launch_record_not_the_callback_request(
        self, client: TestClient, ehr: FakeAuthorizationServer, fake_redis: FakeRedis
    ) -> None:
        """The initiator declares it; nothing about the callback's own request does.

        Asserted by reading the pending record: this is what makes the signal
        travel with ``state`` rather than being inferred from an ``Accept``
        header or a ``User-Agent`` at callback time.
        """
        redirect_url = client.get(
            "/fhir/launch",
            params={"iss": GENERIC_ISS, "delivery": "mobile"},
        ).headers["location"]
        state = parse_qs(urlsplit(redirect_url).query)["state"][0]

        pending = PendingLaunch.model_validate_json(fake_redis.values[launch_key(state)])
        assert pending.delivery is LaunchDelivery.MOBILE


class TestAClaimIsRedeemableExactlyOnce:
    """The acceptance test TASK-051f names first."""

    def test_the_first_redemption_returns_the_launch(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        _, response = complete_launch(client, ehr, delivery="web")
        claim = claim_from(response.headers["location"])

        redeemed = client.post("/fhir/launch/claim", json={"claim": claim})

        assert redeemed.status_code == status.HTTP_200_OK, redeemed.text
        body = redeemed.json()
        assert body["error"] is None
        assert body["data"]["launch_id"]
        assert body["data"]["ehr_type"] == EHRType.GENERIC
        assert body["data"]["expires_in"] == 3600

    def test_the_second_redemption_is_a_404_and_not_a_second_handle(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        _, response = complete_launch(client, ehr, delivery="web")
        claim = claim_from(response.headers["location"])

        first = client.post("/fhir/launch/claim", json={"claim": claim})
        second = client.post("/fhir/launch/claim", json={"claim": claim})

        assert first.status_code == status.HTTP_200_OK
        assert second.status_code == status.HTTP_404_NOT_FOUND, second.text
        assert second.json()["data"] is None
        assert second.json()["error"]["code"] == "SMART_UNKNOWN_CLAIM"

    def test_an_unknown_claim_is_the_same_answer_as_a_redeemed_one(
        self, client: TestClient
    ) -> None:
        """Telling them apart would tell a caller probing codes which were real."""
        response = client.post("/fhir/launch/claim", json={"claim": "never-issued"})

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"]["code"] == "SMART_UNKNOWN_CLAIM"

    def test_the_redeemed_launch_id_is_the_one_the_launch_produced(
        self, client: TestClient, ehr: FakeAuthorizationServer, fake_redis: FakeRedis
    ) -> None:
        """The handoff must name the launch whose token was actually stored."""
        state, response = complete_launch(client, ehr, delivery="mobile")
        claim = claim_from(response.headers["location"])

        launch_id = client.post("/fhir/launch/claim", json={"claim": claim}).json()["data"][
            "launch_id"
        ]

        assert f"fhir_token:{launch_id}" in fake_redis.values

    def test_the_claim_is_held_under_the_configured_short_ttl(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        fake_redis: FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """It bounds a browser redirect reaching an app, not the launch itself.

        Asserted because the TTL is the cheapest lever on how long an intercepted
        code stays useful, and a claim written under the launch record's TTL
        instead would be a silent widening of that window.
        """
        monkeypatch.setenv("SMART_LAUNCH_CLAIM_TTL_SECONDS", "45")
        get_settings.cache_clear()

        _, response = complete_launch(client, ehr, delivery="web")
        claim = claim_from(response.headers["location"])

        assert fake_redis.expiries[launch_claim_key(claim)] == 45

    def test_redemption_discloses_no_credential(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        """The handoff is a way to learn a launch_id, not a route to the token."""
        _, response = complete_launch(client, ehr, delivery="web")
        claim = claim_from(response.headers["location"])

        body = client.post("/fhir/launch/claim", json={"claim": claim}).json()

        assert set(body["data"]) == {"launch_id", "ehr_type", "expires_in"}
        serialized = str(body)
        assert "ehr-access-token" not in serialized
        assert "refresh" not in serialized.lower()


class TestNoLaunchIdReachesALogLineOrAUrl:
    """The second acceptance test TASK-051f names."""

    def test_the_redirect_url_carries_a_claim_and_never_the_launch_id(
        self, client: TestClient, ehr: FakeAuthorizationServer
    ) -> None:
        _, response = complete_launch(client, ehr, delivery="web")
        location = response.headers["location"]
        claim = claim_from(location)

        launch_id = client.post("/fhir/launch/claim", json={"claim": claim}).json()["data"][
            "launch_id"
        ]

        assert launch_id not in location
        assert "launch_id" not in location

    def test_no_log_line_in_the_whole_flow_names_the_launch_id_or_the_claim(
        self,
        client: TestClient,
        ehr: FakeAuthorizationServer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            _, response = complete_launch(client, ehr, delivery="mobile")
            claim = claim_from(response.headers["location"])
            launch_id = client.post("/fhir/launch/claim", json={"claim": claim}).json()["data"][
                "launch_id"
            ]

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert launch_id not in logged
        assert claim not in logged

    def test_a_rejected_claim_is_not_echoed_into_the_log(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Short-lived or not, a presented code is a credential."""
        presented = "a-code-somebody-guessed"

        with caplog.at_level(logging.DEBUG):
            client.post("/fhir/launch/claim", json={"claim": presented})

        assert presented not in "\n".join(record.getMessage() for record in caplog.records)


class TestReturnTargetValidation:
    """Malformed configuration fails at boot, never at the end of a redirect chain."""

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "not-a-url",
            "/launch",
            "http://app.medauth.test/launch",
            "ftp://app.medauth.test/launch",
            "https://app.medauth.test/launch?next=1",
            "https://app.medauth.test/launch#done",
        ],
    )
    def test_a_bad_web_return_url_is_refused(self, value: str) -> None:
        with pytest.raises(ReturnTargetError) as raised:
            validate_web_return_url(value)

        assert "SMART_WEB_RETURN_URL" in str(raised.value)

    @pytest.mark.parametrize(
        "value",
        ["https://app.medauth.test/launch", "http://localhost:5173/launch"],
    )
    def test_an_acceptable_web_return_url_passes(self, value: str) -> None:
        assert validate_web_return_url(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "launch",
            "https://app.medauth.test/launch",
            "http://localhost/launch",
            "medauth://launch?claim=already",
            "medauth://launch#done",
        ],
    )
    def test_a_bad_mobile_return_uri_is_refused(self, value: str) -> None:
        with pytest.raises(ReturnTargetError) as raised:
            validate_mobile_return_uri(value)

        assert "SMART_MOBILE_RETURN_URI" in str(raised.value)

    def test_a_custom_scheme_passes(self) -> None:
        assert validate_mobile_return_uri("medauth://launch") == "medauth://launch"

    def test_startup_names_the_variable_that_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of validating at startup: the error says what to set."""
        monkeypatch.delenv("SMART_WEB_RETURN_URL", raising=False)
        monkeypatch.setenv("SMART_MOBILE_RETURN_URI", TEST_MOBILE_RETURN_URI)

        with pytest.raises(ReturnTargetError) as raised:
            validate_return_targets(Settings())

        assert "SMART_WEB_RETURN_URL" in str(raised.value)

    def test_startup_refuses_a_mobile_target_that_is_a_web_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure this catches is a launch that opens a page, not the app."""
        monkeypatch.setenv("SMART_WEB_RETURN_URL", TEST_WEB_RETURN_URL)
        monkeypatch.setenv("SMART_MOBILE_RETURN_URI", "https://app.medauth.test/launch")

        with pytest.raises(ReturnTargetError) as raised:
            validate_return_targets(Settings())

        assert "SMART_MOBILE_RETURN_URI" in str(raised.value)

    def test_a_configured_deployment_starts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMART_WEB_RETURN_URL", "https://app.medauth.test/launch")
        monkeypatch.setenv("SMART_MOBILE_RETURN_URI", TEST_MOBILE_RETURN_URI)

        validate_return_targets(Settings())


class TestDeliveryUnits:
    """The two helpers the callback composes, checked directly."""

    def test_json_resolves_to_no_redirect_target(self) -> None:
        assert (
            redirect_target_for(
                LaunchDelivery.JSON,
                web_return_url=TEST_WEB_RETURN_URL,
                mobile_return_uri=TEST_MOBILE_RETURN_URI,
            )
            is None
        )

    def test_each_client_delivery_resolves_to_its_own_target(self) -> None:
        assert (
            redirect_target_for(
                LaunchDelivery.WEB,
                web_return_url=TEST_WEB_RETURN_URL,
                mobile_return_uri=TEST_MOBILE_RETURN_URI,
            )
            == TEST_WEB_RETURN_URL
        )
        assert (
            redirect_target_for(
                LaunchDelivery.MOBILE,
                web_return_url=TEST_WEB_RETURN_URL,
                mobile_return_uri=TEST_MOBILE_RETURN_URI,
            )
            == TEST_MOBILE_RETURN_URI
        )

    def test_appending_a_claim_leaves_a_custom_scheme_intact(self) -> None:
        """``urlunsplit`` does not reliably round-trip a non-hierarchical scheme."""
        assert append_claim("medauth://launch", "abc") == "medauth://launch?claim=abc"

    def test_the_delivery_vocabulary_is_exactly_these_three(self) -> None:
        assert {member.value for member in LaunchDelivery} == {"json", "web", "mobile"}

"""Unit tests for the launch flow's pieces, away from the routes.

The route tests exercise these through a whole launch. These cover the edges a
whole launch does not reach: PKCE's encoding rules, the issuer readings, and the
discovery parser's own failure paths.
"""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest

from src.adapters.factory import EHRType
from src.smart.discovery import (
    SMART_CONFIGURATION_PATH,
    DiscoveryError,
    discovery_url,
    fetch_smart_configuration,
)
from src.smart.issuer import issuer_host, normalize_fhir_base_url
from src.smart.oauth import DEFAULT_TOKEN_TTL_SECONDS, TokenResponse
from src.smart.pkce import (
    CODE_CHALLENGE_METHOD,
    derive_code_challenge,
    generate_code_verifier,
)
from src.smart.store import (
    LaunchToken,
    access_token_expiry,
    access_token_is_stale,
    record_ttl_seconds,
)


class TestPkce:
    def test_the_method_is_s256_and_never_plain(self) -> None:
        """`plain` sends the verifier in clear, which is what PKCE exists to avoid."""
        assert CODE_CHALLENGE_METHOD == "S256"

    def test_a_verifier_is_within_the_rfc_length_bounds(self) -> None:
        verifier = generate_code_verifier()

        assert 43 <= len(verifier) <= 128

    def test_a_verifier_is_unpadded_base64url(self) -> None:
        verifier = generate_code_verifier()

        assert "=" not in verifier
        assert "+" not in verifier
        assert "/" not in verifier

    def test_verifiers_are_not_predictable(self) -> None:
        assert len({generate_code_verifier() for _ in range(50)}) == 50

    def test_the_challenge_is_the_unpadded_base64url_sha256(self) -> None:
        verifier = "a-known-verifier-value-for-this-test-000000"
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )

        assert derive_code_challenge(verifier) == expected


class TestIssuerReadings:
    @pytest.mark.parametrize(
        ("iss", "expected"),
        [
            ("https://fhir.example.org/r4", "https://fhir.example.org/r4"),
            ("https://fhir.example.org/r4/", "https://fhir.example.org/r4"),
            ("https://fhir.example.org/r4?practice=7", "https://fhir.example.org/r4"),
            ("https://fhir.example.org/r4#frag", "https://fhir.example.org/r4"),
            ("  https://fhir.example.org/r4  ", "https://fhir.example.org/r4"),
        ],
    )
    def test_normalisation_strips_query_fragment_and_trailing_slash(
        self, iss: str, expected: str
    ) -> None:
        assert normalize_fhir_base_url(iss) == expected

    def test_a_normalised_iss_builds_a_usable_discovery_url(self) -> None:
        """The bug this normalisation exists for: a query string used to land mid-path."""
        url = discovery_url(normalize_fhir_base_url("https://fhir.example.org/r4?practice=7"))

        assert url == f"https://fhir.example.org/r4/{SMART_CONFIGURATION_PATH}"

    def test_the_host_is_read_without_the_rest_of_the_url(self) -> None:
        assert issuer_host("https://fhir.example.org/r4?practice=secret") == "fhir.example.org"

    def test_a_malformed_issuer_yields_an_empty_host_rather_than_raising(self) -> None:
        """A bad iss is a 502 with a message, not a crash inside the logging call."""
        assert issuer_host("not a url") == ""


class TestDiscoveryFailures:
    @staticmethod
    def _client(handler: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_transport_failure_names_the_document_and_host(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with pytest.raises(DiscoveryError) as caught:
            await fetch_smart_configuration(
                self._client(handler), "https://fhir.example.org/r4", issuer_host="fhir.example.org"
            )

        assert SMART_CONFIGURATION_PATH in str(caught.value)
        assert "fhir.example.org" in str(caught.value)

    @pytest.mark.asyncio
    async def test_the_message_names_the_unsupported_older_pattern(self) -> None:
        """So a sandbox that only offers oauth-uris says why in one line."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        with pytest.raises(DiscoveryError) as caught:
            await fetch_smart_configuration(
                self._client(handler), "https://fhir.example.org/r4", issuer_host="fhir.example.org"
            )

        assert "CapabilityStatement" in str(caught.value)

    @pytest.mark.asyncio
    async def test_an_empty_endpoint_is_rejected_rather_than_used(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"authorization_endpoint": "", "token_endpoint": "https://t"}
            )

        with pytest.raises(DiscoveryError):
            await fetch_smart_configuration(
                self._client(handler), "https://fhir.example.org/r4", issuer_host="fhir.example.org"
            )

    @pytest.mark.asyncio
    async def test_unknown_fields_in_the_document_are_ignored(self) -> None:
        """A vendor omitting a field this service never reads must not break a launch."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://a",
                    "token_endpoint": "https://t",
                    "capabilities": ["launch-ehr"],
                    "grant_types_supported": ["authorization_code"],
                },
            )

        configuration = await fetch_smart_configuration(
            self._client(handler), "https://fhir.example.org/r4", issuer_host="fhir.example.org"
        )

        assert configuration.token_endpoint == "https://t"


class TestTokenTtl:
    def test_a_positive_expires_in_is_used(self) -> None:
        assert TokenResponse(access_token="t", expires_in=1800).ttl_seconds == 1800

    @pytest.mark.parametrize("expires_in", [None, 0, -1])
    def test_a_missing_or_nonsensical_expiry_falls_back_to_the_short_floor(
        self, expires_in: int | None
    ) -> None:
        """A record with no TTL would outlive the credential it holds."""
        token = TokenResponse(access_token="t", expires_in=expires_in)

        assert token.ttl_seconds == DEFAULT_TOKEN_TTL_SECONDS


class TestTheRecordTtlRule:
    """TASK-051b's storage fix, at the level of the function that decides it.

    The route suites prove the fix end to end; these cover the arithmetic and
    the two branches directly, because the whole defect was one lifetime being
    used to represent two.
    """

    def _record(self, *, expires_in: int, refresh_token: str | None) -> LaunchToken:
        return LaunchToken(
            ehr_type=EHRType.GENERIC,
            fhir_base_url="https://fhir.example.org/r4",
            access_token="token",
            access_token_expires_at=access_token_expiry(expires_in),
            token_endpoint="https://auth.example.org/token",
            refresh_token=refresh_token,
        )

    def test_a_renewable_record_outlives_its_access_token(self) -> None:
        record = self._record(expires_in=3600, refresh_token="grant")

        assert record_ttl_seconds(record, refresh_grant_ttl_seconds=28800) == 28800

    def test_a_record_with_no_grant_expires_with_its_access_token(self) -> None:
        record = self._record(expires_in=900, refresh_token=None)

        assert record_ttl_seconds(record, refresh_grant_ttl_seconds=28800) == 900

    def test_an_access_token_outliving_the_grant_bound_still_wins(self) -> None:
        """An EHR issuing a token longer-lived than our bound must not be truncated."""
        record = self._record(expires_in=86400, refresh_token="grant")

        assert record_ttl_seconds(record, refresh_grant_ttl_seconds=28800) == 86400

    def test_an_already_expired_record_never_asks_redis_for_a_zero_ttl(self) -> None:
        """Redis rejects a non-positive TTL, so the floor is one second, not zero."""
        record = self._record(expires_in=-500, refresh_token=None)

        assert record_ttl_seconds(record, refresh_grant_ttl_seconds=28800) == 1


class TestWhenAnAccessTokenIsStale:
    def _record(self, *, expires_in: int) -> LaunchToken:
        return LaunchToken(
            ehr_type=EHRType.GENERIC,
            fhir_base_url="https://fhir.example.org/r4",
            access_token="token",
            access_token_expires_at=access_token_expiry(expires_in),
            token_endpoint="https://auth.example.org/token",
        )

    @pytest.mark.parametrize("expires_in", [-60, 0, 60])
    def test_expired_or_inside_the_margin_is_stale(self, expires_in: int) -> None:
        """The margin covers clock skew and the round trip about to be made."""
        assert access_token_is_stale(self._record(expires_in=expires_in), skew_seconds=120)

    def test_comfortably_live_is_not_stale(self) -> None:
        assert not access_token_is_stale(self._record(expires_in=3600), skew_seconds=120)

    def test_a_zero_margin_renews_only_once_the_token_is_actually_expired(self) -> None:
        """The margin is configuration, and zero must mean zero."""
        assert not access_token_is_stale(self._record(expires_in=5), skew_seconds=0)
        assert access_token_is_stale(self._record(expires_in=-5), skew_seconds=0)

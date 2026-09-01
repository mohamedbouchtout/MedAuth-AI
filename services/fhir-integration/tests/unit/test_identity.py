"""Unit tests for ``src.smart.identity`` — TASK-051c.

The rule every test here circles: an actor is recorded only when the EHR both
asserted it and proved the assertion. Anything short of that is ``None``, never
the unverified claim, and never a failed launch.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from src.smart.identity import (
    ACTOR_RESOURCE_TYPE,
    SUPPORTED_ID_TOKEN_ALGORITHMS,
    resolve_launch_actor,
)
from tests.unit.idtokens import (
    AUDIENCE,
    ISSUER,
    JWKS_URI,
    KEY_ID,
    id_token,
    jwks,
)

FHIR_BASE_URL = "https://fhir.example-hospital.org/r4"
ISSUER_HOST = "auth.example-hospital.org"
EXPECTED_REFERENCE = f"{FHIR_BASE_URL}/Practitioner/prov-77"


def jwks_client(
    body: dict[str, Any] | str | None = None,
    *,
    status_code: int = 200,
) -> httpx.AsyncClient:
    """A client that serves one key set at ``JWKS_URI``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) != JWKS_URI:
            return httpx.Response(404, json={"error": "not_found"})
        payload = jwks() if body is None else body
        if isinstance(payload, str):
            return httpx.Response(status_code, text=payload)
        return httpx.Response(status_code, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def resolve(
    client: httpx.AsyncClient,
    *,
    token: str | None,
    jwks_uri: str | None = JWKS_URI,
    oidc_issuer: str | None = ISSUER,
    audience: str = AUDIENCE,
) -> str | None:
    """Call the module under test with the ordinary arguments."""
    return await resolve_launch_actor(
        client,
        id_token=token,
        jwks_uri=jwks_uri,
        oidc_issuer=oidc_issuer,
        audience=audience,
        fhir_base_url=FHIR_BASE_URL,
        issuer_host=ISSUER_HOST,
    )


class TestAVerifiableClaim:
    async def test_it_resolves_to_an_absolute_practitioner_reference(self) -> None:
        async with jwks_client() as client:
            actor = await resolve(client, token=id_token())

        assert actor == EXPECTED_REFERENCE

    async def test_an_absolute_claim_is_kept_as_the_ehr_wrote_it(self) -> None:
        """Not re-derived against our own base URL.

        An EHR that names a full URL has said which server the provider lives
        on, and rewriting it would be this service overruling that.
        """
        absolute = "https://identity.example-hospital.org/fhir/Practitioner/prov-77"

        async with jwks_client() as client:
            actor = await resolve(client, token=id_token(fhir_user=absolute))

        assert actor == absolute

    async def test_the_key_is_selected_by_kid(self) -> None:
        """A set holding a non-matching key as well still verifies."""
        set_with_two = jwks()
        other = jwks(kid="some-other-key", tag="secondary")
        set_with_two["keys"] = [*other["keys"], *set_with_two["keys"]]

        async with jwks_client(set_with_two) as client:
            actor = await resolve(client, token=id_token(kid=KEY_ID))

        assert actor == EXPECTED_REFERENCE

    async def test_a_token_with_no_kid_verifies_against_a_single_key(self) -> None:
        async with jwks_client() as client:
            actor = await resolve(client, token=id_token(kid=None))

        assert actor == EXPECTED_REFERENCE


class TestTheActorStaysUnknown:
    """Every one of these is a null actor and a launch that still works."""

    async def test_when_the_ehr_sent_no_id_token(self) -> None:
        async with jwks_client() as client:
            assert await resolve(client, token=None) is None

    async def test_when_the_ehr_publishes_no_key_set(self) -> None:
        """SMART marks `jwks_uri` conditional, so this is a conformant server."""
        async with jwks_client() as client:
            assert await resolve(client, token=id_token(), jwks_uri=None) is None

    async def test_when_the_ehr_publishes_no_issuer(self) -> None:
        async with jwks_client() as client:
            assert await resolve(client, token=id_token(), oidc_issuer=None) is None

    async def test_when_the_key_set_cannot_be_fetched(self) -> None:
        async with jwks_client(status_code=503) as client:
            assert await resolve(client, token=id_token()) is None

    async def test_when_the_key_set_is_not_json(self) -> None:
        async with jwks_client("<html>maintenance</html>") as client:
            assert await resolve(client, token=id_token()) is None

    async def test_when_the_key_set_is_empty(self) -> None:
        async with jwks_client({"keys": []}) as client:
            assert await resolve(client, token=id_token()) is None

    async def test_when_no_key_matches_the_token_kid(self) -> None:
        async with jwks_client(jwks(kid="a-different-key")) as client:
            assert await resolve(client, token=id_token(kid=KEY_ID)) is None

    async def test_when_the_token_has_no_kid_and_the_set_has_several(self) -> None:
        """Choosing between keys without a `kid` would be a guess, not a check."""
        two = jwks()
        two["keys"] = [*jwks(kid="another", tag="secondary")["keys"], *two["keys"]]

        async with jwks_client(two) as client:
            assert await resolve(client, token=id_token(kid=None)) is None

    async def test_when_the_signature_is_from_a_different_key(self) -> None:
        """The test the whole module exists for."""
        async with jwks_client() as client:
            assert await resolve(client, token=id_token(tag="secondary")) is None

    async def test_when_the_token_has_expired(self) -> None:
        async with jwks_client() as client:
            assert await resolve(client, token=id_token(expires_in=-60)) is None

    async def test_when_the_audience_is_another_client(self) -> None:
        """A token minted for a different app is not ours to read an identity from."""
        async with jwks_client() as client:
            assert await resolve(client, token=id_token(audience="some-other-app")) is None

    async def test_when_the_issuer_is_not_the_one_discovery_named(self) -> None:
        async with jwks_client() as client:
            assert await resolve(client, token=id_token(issuer="https://evil.example")) is None

    async def test_when_the_token_carries_no_fhir_user(self) -> None:
        async with jwks_client() as client:
            assert await resolve(client, token=id_token(fhir_user=None)) is None

    async def test_when_the_fhir_user_claim_is_blank(self) -> None:
        async with jwks_client() as client:
            assert await resolve(client, token=id_token(fhir_user="   ")) is None

    @pytest.mark.parametrize(
        "claim",
        [
            "Patient/synthea-123",
            "RelatedPerson/rp-1",
            "Person/p-1",
            "https://fhir.example-hospital.org/r4/Patient/synthea-123",
        ],
    )
    async def test_when_the_claim_does_not_reference_a_practitioner(self, claim: str) -> None:
        """An actor column is not a place for a patient identifier.

        `fhirUser` may name a Patient for a patient-facing app; this is not one,
        and writing that reference into fhir_practitioner_ref would both misname
        the value and put PHI in a column nothing treats as PHI.
        """
        async with jwks_client() as client:
            assert await resolve(client, token=id_token(fhir_user=claim)) is None

    async def test_when_the_claim_names_a_practitioner_with_no_id(self) -> None:
        async with jwks_client() as client:
            assert await resolve(client, token=id_token(fhir_user="Practitioner/")) is None

    async def test_the_smart_v1_profile_claim_is_not_read_as_a_fallback(self) -> None:
        """Deliberately unsupported — see the comment at the claim read.

        SMART 1.0 carried this in `profile`. Accepting it here would mean
        trusting a source nobody has checked against a real 2.0 server.
        """
        token = id_token(fhir_user=None, extra_claims={"profile": "Practitioner/prov-77"})

        async with jwks_client() as client:
            assert await resolve(client, token=token) is None


class TestItNeverLeaksTheCredential:
    async def test_no_log_line_carries_the_token_or_the_claim(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A refusal is a reason, never the material that was refused."""
        caplog.set_level(logging.DEBUG)
        token = id_token(tag="secondary")

        async with jwks_client() as client:
            assert await resolve(client, token=token) is None

        # Every record, not only this module's: a credential leaking through
        # somebody else's logger is still a credential in the log.
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert token not in logged
        assert "prov-77" not in logged

    async def test_a_successful_resolution_logs_no_reference_either(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)

        async with jwks_client() as client:
            actor = await resolve(client, token=id_token())

        assert actor is not None
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "prov-77" not in logged


class TestTheAlgorithmAllowList:
    def test_it_admits_no_symmetric_or_none_algorithm(self) -> None:
        """The classic algorithm-confusion hole, closed by construction."""
        assert "none" not in SUPPORTED_ID_TOKEN_ALGORITHMS
        assert not [alg for alg in SUPPORTED_ID_TOKEN_ALGORITHMS if alg.startswith("HS")]

    async def test_an_unsigned_token_is_refused(self) -> None:
        """`alg: none` is the shape an attacker reaches for first."""
        import jwt as pyjwt

        unsigned = pyjwt.encode(
            {"iss": ISSUER, "aud": AUDIENCE, "exp": 9999999999, "fhirUser": "Practitioner/x"},
            key="",
            algorithm="none",
        )

        async with jwks_client() as client:
            assert await resolve(client, token=unsigned) is None


def test_only_practitioner_is_accepted_as_an_actor() -> None:
    """Pinned so widening it becomes a deliberate edit with a reason."""
    assert ACTOR_RESOURCE_TYPE == "Practitioner"

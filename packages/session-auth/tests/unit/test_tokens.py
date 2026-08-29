"""Token validation: the four checks, and the reason label each failure carries.

The route tests in each consuming service prove a bad token is refused. These
prove *why* each one is bad.
"""

from __future__ import annotations

import datetime
import uuid

import jwt
import pytest

from session_auth import (
    JWT_ALGORITHM,
    MIN_SIGNING_KEY_BYTES,
    SessionAuthError,
    validate_token,
)

KEY = "auth-unit-test-signing-key-32-bytes"


def mint(
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None = None,
    key: str = KEY,
    lifetime_seconds: int = 900,
    claims: dict[str, object] | None = None,
) -> str:
    body: dict[str, object] = {
        "session_id": str(session_id),
        "provider_id": str(provider_id or uuid.uuid4()),
        "exp": int(
            (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=lifetime_seconds)
            ).timestamp()
        ),
    }
    if claims is not None:
        body = claims
    return jwt.encode(body, key, algorithm=JWT_ALGORITHM)


class TestConstants:
    """The two values every consuming service configures itself from."""

    def test_the_floor_matches_the_hs256_digest_length(self) -> None:
        assert MIN_SIGNING_KEY_BYTES == 32

    def test_the_algorithm_is_pinned_rather_than_negotiated(self) -> None:
        """Honouring a token's own ``alg`` is the classic JWT bypass."""
        assert JWT_ALGORITHM == "HS256"


class TestValidation:
    """The four checks, and the reason label each failure carries."""

    def test_a_good_token_yields_the_session_and_provider(self) -> None:
        session_id = uuid.uuid4()
        provider_id = uuid.uuid4()

        identity = validate_token(
            mint(session_id=session_id, provider_id=provider_id),
            session_id=str(session_id),
            signing_key=KEY,
        )

        assert identity.session_id == session_id
        assert identity.provider_id == provider_id

    def test_an_expired_token_is_refused(self) -> None:
        session_id = uuid.uuid4()

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=session_id, lifetime_seconds=-1),
                session_id=str(session_id),
                signing_key=KEY,
            )

        assert refusal.value.reason == "expired"

    def test_a_token_signed_with_another_key_is_refused(self) -> None:
        session_id = uuid.uuid4()

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=session_id, key="a-different-signing-key-32-bytes!"),
                session_id=str(session_id),
                signing_key=KEY,
            )

        assert refusal.value.reason == "invalid_token"

    def test_an_unsigned_token_is_refused(self) -> None:
        """``alg: none`` is the classic JWT bypass; the algorithm is pinned.

        This is why :mod:`session_auth.tokens` fixes ``JWT_ALGORITHM`` rather
        than reading the header of the token being validated.
        """
        session_id = uuid.uuid4()
        unsigned = jwt.encode(
            {"session_id": str(session_id), "provider_id": str(uuid.uuid4()), "exp": 9_999_999_999},
            key="",
            algorithm="none",
        )

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(unsigned, session_id=str(session_id), signing_key=KEY)

        assert refusal.value.reason == "invalid_token"

    def test_a_token_for_another_session_is_refused(self) -> None:
        """The check that stops one encounter's token opening another's socket."""
        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=uuid.uuid4()),
                session_id=str(uuid.uuid4()),
                signing_key=KEY,
            )

        assert refusal.value.reason == "session_mismatch"

    def test_the_session_claim_is_compared_as_a_uuid_not_as_text(self) -> None:
        """The same session in different capitalisation is the same session."""
        session_id = uuid.uuid4()

        identity = validate_token(
            mint(session_id=session_id),
            session_id=str(session_id).upper(),
            signing_key=KEY,
        )

        assert identity.session_id == session_id

    @pytest.mark.parametrize(
        "claims",
        [
            {"provider_id": str(uuid.uuid4()), "exp": 9_999_999_999},
            {"session_id": str(uuid.uuid4()), "exp": 9_999_999_999},
            {"session_id": str(uuid.uuid4()), "provider_id": str(uuid.uuid4())},
        ],
        ids=["no session_id", "no provider_id", "no exp"],
    )
    def test_a_token_missing_a_required_claim_is_refused(self, claims: dict[str, object]) -> None:
        """A token without ``exp`` is a token that never expires."""
        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=uuid.uuid4(), claims=claims),
                session_id=str(claims.get("session_id", uuid.uuid4())),
                signing_key=KEY,
            )

        assert refusal.value.reason == "missing_claim"

    def test_a_claim_that_is_not_a_uuid_is_refused(self) -> None:
        session_id = uuid.uuid4()
        claims = {
            "session_id": str(session_id),
            "provider_id": "not-a-uuid",
            "exp": 9_999_999_999,
        }

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=session_id, claims=claims),
                session_id=str(session_id),
                signing_key=KEY,
            )

        assert refusal.value.reason == "malformed_claim"

    def test_a_url_session_id_that_is_not_a_uuid_is_refused(self) -> None:
        session_id = uuid.uuid4()

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=session_id),
                session_id="../../etc/passwd",
                signing_key=KEY,
            )

        assert refusal.value.reason == "malformed_session_id"

    def test_a_refusal_reason_never_carries_the_token(self) -> None:
        """The reason is logged. A token in it would be a credential in a log."""
        session_id = uuid.uuid4()
        token = mint(session_id=session_id, lifetime_seconds=-1)

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(token, session_id=str(session_id), signing_key=KEY)

        assert token not in str(refusal.value)
        assert token not in refusal.value.reason

"""What a token has to be to authorise re-minting its own session (TASK-006b).

The route tests prove a bad credential ends in 401. These prove *which* tokens
are bad and why, and — the point of the whole design — that the one check the
re-mint validator relaxes is expiry and nothing else. Every other check it
performs is the one ``audio-ingestion``'s ``validate_token`` performs, because a
re-mint endpoint weaker than the socket it feeds would be the hole in the wall.
"""

from __future__ import annotations

import datetime
import uuid

import jwt
import pytest

from track_a_clinical.config import JWT_ALGORITHM, Settings
from track_a_clinical.session_tokens import (
    RemintCredentialError,
    validate_remint_credential,
)

SIGNING_KEY = "remint-unit-test-key-padded-to-32b"
NOW = datetime.datetime(2026, 8, 18, 12, 0, tzinfo=datetime.UTC)


def make_settings(*, grace: int = 3600, key: str = SIGNING_KEY) -> Settings:
    return Settings(jwt_signing_key=key, session_remint_grace_seconds=grace)


def mint(
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None = None,
    expires_at: datetime.datetime = NOW,
    key: str = SIGNING_KEY,
    claims: dict[str, object] | None = None,
) -> str:
    """Mint a token by hand so a test can build one the service never would."""
    body: dict[str, object] = {
        "session_id": str(session_id),
        "provider_id": str(provider_id or uuid.uuid4()),
        "exp": int(expires_at.timestamp()),
    }
    if claims is not None:
        body = claims
    return jwt.encode(body, key, algorithm=JWT_ALGORITHM)


class TestExpiryIsTheOnlyRelaxedCheck:
    """An expired token is the *reason* a client is here, within a bound."""

    def test_a_still_valid_token_is_accepted(self) -> None:
        """Clients are told to refresh proactively, so this is the common path."""
        session_id = uuid.uuid4()
        token = mint(session_id=session_id, expires_at=NOW + datetime.timedelta(minutes=5))

        claims = validate_remint_credential(
            token, session_id=session_id, settings=make_settings(), now=NOW
        )

        assert claims["session_id"] == str(session_id)

    def test_a_token_expired_inside_the_grace_window_is_accepted(self) -> None:
        session_id = uuid.uuid4()
        token = mint(session_id=session_id, expires_at=NOW - datetime.timedelta(minutes=59))

        claims = validate_remint_credential(
            token, session_id=session_id, settings=make_settings(grace=3600), now=NOW
        )

        assert claims["session_id"] == str(session_id)

    def test_a_token_expired_beyond_the_grace_window_is_refused(self) -> None:
        """The bound on how long one captured token stays useful."""
        session_id = uuid.uuid4()
        token = mint(session_id=session_id, expires_at=NOW - datetime.timedelta(minutes=61))

        with pytest.raises(RemintCredentialError) as raised:
            validate_remint_credential(
                token, session_id=session_id, settings=make_settings(grace=3600), now=NOW
            )

        assert raised.value.reason == "expired_beyond_grace"

    def test_the_window_is_measured_from_the_configured_value(self) -> None:
        """Not a hardcoded hour — the same token flips on the setting alone."""
        session_id = uuid.uuid4()
        token = mint(session_id=session_id, expires_at=NOW - datetime.timedelta(minutes=30))

        validate_remint_credential(
            token, session_id=session_id, settings=make_settings(grace=3600), now=NOW
        )
        with pytest.raises(RemintCredentialError):
            validate_remint_credential(
                token, session_id=session_id, settings=make_settings(grace=60), now=NOW
            )


class TestEveryOtherCheckStillApplies:
    """Relaxing expiry must not have relaxed anything else along with it."""

    def test_a_token_signed_with_another_key_is_refused(self) -> None:
        session_id = uuid.uuid4()
        token = mint(session_id=session_id, key="a-different-key-also-32-bytes-long")

        with pytest.raises(RemintCredentialError) as raised:
            validate_remint_credential(
                token, session_id=session_id, settings=make_settings(), now=NOW
            )

        assert raised.value.reason == "invalid_token"

    def test_an_unsigned_token_is_refused(self) -> None:
        """alg=none must not slip past a validator that skips expiry."""
        session_id = uuid.uuid4()
        token = jwt.encode(
            {"session_id": str(session_id), "provider_id": str(uuid.uuid4()), "exp": 0},
            key="",
            algorithm="none",
        )

        with pytest.raises(RemintCredentialError) as raised:
            validate_remint_credential(
                token, session_id=session_id, settings=make_settings(), now=NOW
            )

        assert raised.value.reason == "invalid_token"

    def test_a_token_for_another_session_is_refused(self) -> None:
        """The check that stops one encounter's token minting another's."""
        token = mint(session_id=uuid.uuid4())

        with pytest.raises(RemintCredentialError) as raised:
            validate_remint_credential(
                token, session_id=uuid.uuid4(), settings=make_settings(), now=NOW
            )

        assert raised.value.reason == "session_mismatch"

    def test_the_session_claim_is_compared_as_a_uuid_not_as_text(self) -> None:
        """Different capitalisation is the same session; rejecting it is an outage."""
        session_id = uuid.uuid4()
        token = mint(
            session_id=session_id,
            claims={
                "session_id": str(session_id).upper(),
                "provider_id": str(uuid.uuid4()),
                "exp": int(NOW.timestamp()),
            },
        )

        claims = validate_remint_credential(
            token, session_id=session_id, settings=make_settings(), now=NOW
        )

        assert uuid.UUID(claims["session_id"]) == session_id

    @pytest.mark.parametrize(
        "claims",
        [
            {"provider_id": str(uuid.uuid4()), "exp": 0},
            {"session_id": str(uuid.uuid4()), "exp": 0},
            {"session_id": str(uuid.uuid4()), "provider_id": str(uuid.uuid4())},
        ],
        ids=["no_session_id", "no_provider_id", "no_exp"],
    )
    def test_a_token_missing_a_required_claim_is_refused(self, claims: dict[str, object]) -> None:
        token = mint(session_id=uuid.uuid4(), claims=claims)

        with pytest.raises(RemintCredentialError) as raised:
            validate_remint_credential(
                token, session_id=uuid.uuid4(), settings=make_settings(), now=NOW
            )

        assert raised.value.reason == "missing_claim"

    def test_a_claim_that_is_not_a_uuid_is_refused(self) -> None:
        session_id = uuid.uuid4()
        token = mint(
            session_id=session_id,
            claims={
                "session_id": str(session_id),
                "provider_id": "not-a-uuid",
                "exp": int(NOW.timestamp()),
            },
        )

        with pytest.raises(RemintCredentialError) as raised:
            validate_remint_credential(
                token, session_id=session_id, settings=make_settings(), now=NOW
            )

        assert raised.value.reason == "malformed_claim"

    def test_an_empty_token_is_refused(self) -> None:
        """What a request with no Authorization header hands the validator."""
        with pytest.raises(RemintCredentialError):
            validate_remint_credential(
                "", session_id=uuid.uuid4(), settings=make_settings(), now=NOW
            )


def test_a_refusal_reason_never_carries_the_token() -> None:
    """The reason is logged; the token is a credential and must not ride along."""
    token = mint(session_id=uuid.uuid4())

    with pytest.raises(RemintCredentialError) as raised:
        validate_remint_credential(
            token, session_id=uuid.uuid4(), settings=make_settings(), now=NOW
        )

    assert token not in str(raised.value)
    assert token not in raised.value.reason

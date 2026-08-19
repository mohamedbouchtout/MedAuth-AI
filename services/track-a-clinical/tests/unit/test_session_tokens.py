"""Claims, signature and expiry of the minted session JWT."""

from __future__ import annotations

import datetime
import uuid

import jwt
import pytest

from track_a_clinical.config import JWT_ALGORITHM, Settings
from track_a_clinical.session_tokens import CLAIM_NAMES, mint_session_jwt

#: Long enough to satisfy the minimum this service enforces on HS256 keys.
SIGNING_KEY = "unit-test-signing-key-padded-to-32b"


def make_settings(ttl: int = 900) -> Settings:
    return Settings(jwt_signing_key=SIGNING_KEY, session_ttl_seconds=ttl)


def test_token_carries_exactly_the_v1_claims() -> None:
    session_id = uuid.uuid4()
    provider_id = uuid.uuid4()

    token = mint_session_jwt(
        session_id=session_id, provider_id=provider_id, settings=make_settings()
    )
    claims = jwt.decode(token, SIGNING_KEY, algorithms=[JWT_ALGORITHM])

    assert claims["session_id"] == str(session_id)
    assert claims["provider_id"] == str(provider_id)
    # Exact set, not a superset: TASK-020's and TASK-041's validators are written
    # against these three and nothing else.
    assert set(claims) == CLAIM_NAMES


def test_expiry_matches_the_configured_ttl() -> None:
    issued_at = datetime.datetime(2026, 8, 18, 12, 0, tzinfo=datetime.UTC)

    token = mint_session_jwt(
        session_id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        settings=make_settings(),
        now=issued_at,
    )
    claims = jwt.decode(
        token, SIGNING_KEY, algorithms=[JWT_ALGORITHM], options={"verify_exp": False}
    )

    assert claims["exp"] == int((issued_at + datetime.timedelta(minutes=15)).timestamp())


def test_a_shorter_ttl_shortens_the_token() -> None:
    issued_at = datetime.datetime(2026, 8, 18, 12, 0, tzinfo=datetime.UTC)

    token = mint_session_jwt(
        session_id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        settings=make_settings(ttl=60),
        now=issued_at,
    )
    claims = jwt.decode(
        token, SIGNING_KEY, algorithms=[JWT_ALGORITHM], options={"verify_exp": False}
    )

    assert claims["exp"] == int((issued_at + datetime.timedelta(seconds=60)).timestamp())


def test_expired_token_is_rejected_by_a_downstream_validator() -> None:
    """What audio-ingestion and nudge-service will do with a stale token."""
    long_ago = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    token = mint_session_jwt(
        session_id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        settings=make_settings(),
        now=long_ago,
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(token, SIGNING_KEY, algorithms=[JWT_ALGORITHM])


def test_token_signed_with_another_key_is_rejected() -> None:
    token = mint_session_jwt(
        session_id=uuid.uuid4(), provider_id=uuid.uuid4(), settings=make_settings()
    )

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "a-different-key-padded-to-32-bytes", algorithms=[JWT_ALGORITHM])

"""Settings loading for the session lifecycle endpoints."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from track_a_clinical.config import DEFAULT_SESSION_TTL_SECONDS, get_settings

#: Meets the 32-byte minimum this service enforces on HS256 keys.
TEST_KEY = "config-test-signing-key-padded-32"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Settings are cached process-wide; each test needs its own read."""
    get_settings.cache_clear()


def test_ttl_defaults_to_fifteen_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SIGNING_KEY", TEST_KEY)
    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)

    assert get_settings().session_ttl_seconds == DEFAULT_SESSION_TTL_SECONDS == 900


def test_ttl_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SIGNING_KEY", TEST_KEY)
    monkeypatch.setenv("SESSION_TTL_SECONDS", "60")

    assert get_settings().session_ttl_seconds == 60


def test_missing_signing_key_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key that is absent or blank must fail loudly, never sign with an empty secret."""
    monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)

    with pytest.raises(ValidationError):
        get_settings()


@pytest.mark.parametrize("key", ["", "too-short"])
def test_weak_signing_key_is_an_error(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """HS256 with a secret shorter than the digest is a weak MAC, not a dev convenience."""
    monkeypatch.setenv("JWT_SIGNING_KEY", key)

    with pytest.raises(ValidationError):
        get_settings()


def test_issuer_and_audience_are_not_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 tokens carry no iss/aud, so those env vars must not leak into settings.

    They exist in .env.example from an earlier scaffold pass. Picking them up here
    would be the first step toward claims that TASK-020 and TASK-041 do not validate.
    """
    monkeypatch.setenv("JWT_SIGNING_KEY", TEST_KEY)
    monkeypatch.setenv("JWT_ISSUER", "medauth")
    monkeypatch.setenv("JWT_AUDIENCE", "medauth-clients")

    settings = get_settings()

    assert not hasattr(settings, "jwt_issuer")
    assert not hasattr(settings, "jwt_audience")

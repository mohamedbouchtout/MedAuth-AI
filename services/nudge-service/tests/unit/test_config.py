"""Settings for the nudge relay."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings, get_settings

KEY = "config-unit-test-signing-key-32byt"


def test_a_short_signing_key_is_rejected() -> None:
    """The same floor the issuer enforces, imported from session_auth.

    If this service accepted a key the issuer refuses, a misconfiguration would
    surface as connections that are rejected for no visible reason rather than as
    a startup failure.
    """
    with pytest.raises(ValidationError):
        Settings(jwt_signing_key="too-short")


def test_the_redis_url_has_a_local_default() -> None:
    assert Settings(jwt_signing_key=KEY).redis_url.startswith("redis://")


def test_an_empty_redis_url_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(jwt_signing_key=KEY, redis_url="")


def test_settings_are_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", KEY)

    assert get_settings() is get_settings()
    get_settings.cache_clear()


def test_unrelated_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """One ``.env.local`` serves every service; each reads only its own keys."""
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", KEY)
    monkeypatch.setenv("BEDROCK_MODEL_ID_REASONING", "not-ours")

    assert get_settings().jwt_signing_key == KEY
    get_settings.cache_clear()

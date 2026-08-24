"""Settings, and the two constraints that must agree with track-a-clinical."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import (
    DEFAULT_MEDIA_ENCODING,
    DEFAULT_SAMPLE_RATE_HZ,
    JWT_ALGORITHM,
    MIN_SIGNING_KEY_BYTES,
    Settings,
    get_settings,
)

KEY = "config-unit-test-signing-key-32byt"


def test_a_short_signing_key_is_rejected() -> None:
    """The same floor track-a-clinical enforces on the issuing side.

    If this service accepted a key the issuer refuses, a misconfiguration would
    surface as tokens that never validate rather than as a startup failure.
    """
    with pytest.raises(ValidationError):
        Settings(jwt_signing_key="too-short")


def test_the_floor_matches_the_hs256_digest_length() -> None:
    assert MIN_SIGNING_KEY_BYTES == 32


def test_the_algorithm_is_pinned_rather_than_negotiated() -> None:
    """Honouring a token's own ``alg`` is the classic JWT bypass."""
    assert JWT_ALGORITHM == "HS256"


def test_audio_format_defaults_match_what_the_capture_clients_send() -> None:
    settings = Settings(jwt_signing_key=KEY)

    assert settings.transcribe_medical_sample_rate_hz == DEFAULT_SAMPLE_RATE_HZ == 16_000
    assert settings.transcribe_medical_media_encoding == DEFAULT_MEDIA_ENCODING == "pcm"


def test_transcribe_settings_are_overridable_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A practice in a different specialty changes a variable, not a release."""
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", KEY)
    monkeypatch.setenv("TRANSCRIBE_MEDICAL_SPECIALTY", "CARDIOLOGY")
    monkeypatch.setenv("TRANSCRIBE_MEDICAL_TYPE", "DICTATION")
    monkeypatch.setenv("TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ", "8000")

    settings = get_settings()

    assert settings.transcribe_medical_specialty == "CARDIOLOGY"
    assert settings.transcribe_medical_type == "DICTATION"
    assert settings.transcribe_medical_sample_rate_hz == 8_000
    get_settings.cache_clear()


def test_a_nonsense_sample_rate_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(jwt_signing_key=KEY, transcribe_medical_sample_rate_hz=0)


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

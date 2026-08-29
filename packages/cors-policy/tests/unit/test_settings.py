"""Unit tests for the shared ``AllowedOrigins`` settings field.

The first test here is a regression test with a specific failure in mind. A
``tuple[str, ...]`` settings field without ``NoDecode`` does not merely mis-parse
a comma-separated value — ``pydantic-settings`` tries to JSON-decode it before
any validator runs and raises ``SettingsError``, so the service does not start.
That is easy to reintroduce by "simplifying" the annotation, and the failure
appears at deploy time rather than in a type check.
"""

from __future__ import annotations

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from cors_policy import CorsPolicyError
from cors_policy.settings import AllowedOrigins


class ExampleSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    cors_allowed_origins: AllowedOrigins = ()


def test_comma_separated_environment_value_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173, https://app.example.com")

    assert ExampleSettings().cors_allowed_origins == (
        "http://localhost:5173",
        "https://app.example.com",
    )


def test_unset_environment_leaves_the_empty_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    assert ExampleSettings().cors_allowed_origins == ()


def test_empty_environment_value_is_no_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")

    assert ExampleSettings().cors_allowed_origins == ()


def test_a_tuple_passes_through_for_directly_constructed_settings() -> None:
    """Most tests build settings in code rather than through the environment."""
    settings = ExampleSettings(cors_allowed_origins=("https://app.example.com",))

    assert settings.cors_allowed_origins == ("https://app.example.com",)


def test_wildcard_in_the_environment_stops_the_service_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal reaches the caller as a startup failure, not a warning."""
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")

    with pytest.raises((CorsPolicyError, ValueError)):
        ExampleSettings()

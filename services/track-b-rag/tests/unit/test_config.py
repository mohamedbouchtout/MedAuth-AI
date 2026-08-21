"""Settings read the environment, and every value has a working local default."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from track_b_rag.config import (
    DEFAULT_AWS_REGION,
    DEFAULT_BEDROCK_MODEL_ID_REASONING,
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL_NAME,
    DEFAULT_QDRANT_COLLECTION,
    DEFAULT_REDIS_URL,
    Settings,
    get_settings,
)

QDRANT_VARS = (
    "QDRANT_HOST",
    "QDRANT_PORT",
    "QDRANT_API_KEY",
    "QDRANT_COLLECTION",
    "EMBEDDING_MODEL_NAME",
    "EMBEDDING_DIMENSIONS",
    "REDIS_URL",
    "AWS_REGION",
    "BEDROCK_MODEL_ID_REASONING",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run with none of this service's variables set, and no cached settings."""
    get_settings.cache_clear()
    for name in QDRANT_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
    get_settings.cache_clear()


def test_defaults_match_docker_compose_and_the_tech_stack(clean_env: None) -> None:
    settings = Settings()

    assert settings.qdrant_host == "localhost"
    assert settings.qdrant_port == 6333  # docker-compose.yml publishes 6333 for REST
    assert settings.qdrant_api_key is None
    assert settings.qdrant_collection == DEFAULT_QDRANT_COLLECTION == "insurance_policies"
    assert settings.embedding_model_name == DEFAULT_EMBEDDING_MODEL_NAME
    assert settings.embedding_dimensions == DEFAULT_EMBEDDING_DIMENSIONS == 1024
    assert settings.redis_url == DEFAULT_REDIS_URL == "redis://localhost:6379/0"
    assert settings.aws_region == DEFAULT_AWS_REGION == "us-east-1"
    assert settings.bedrock_model_id_reasoning == DEFAULT_BEDROCK_MODEL_ID_REASONING


def test_values_come_from_the_environment(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_HOST", "qdrant.internal")
    monkeypatch.setenv("QDRANT_PORT", "7333")
    monkeypatch.setenv("QDRANT_COLLECTION", "policies_staging")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")

    settings = Settings()

    assert settings.qdrant_host == "qdrant.internal"
    assert settings.qdrant_port == 7333
    assert settings.qdrant_collection == "policies_staging"
    assert settings.embedding_dimensions == 768


def test_an_empty_api_key_is_unset_not_an_empty_secret(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.env.example ships every key valueless; sourcing it must not fake a key."""
    monkeypatch.setenv("QDRANT_API_KEY", "")

    assert Settings().qdrant_api_key is None


def test_a_real_api_key_survives(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_API_KEY", "s3cret")

    assert Settings().qdrant_api_key == "s3cret"


def test_qdrant_url_is_host_and_port(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_HOST", "qdrant.internal")
    monkeypatch.setenv("QDRANT_PORT", "6334")

    assert Settings().qdrant_url == "http://qdrant.internal:6334"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("QDRANT_PORT", "0"),
        ("QDRANT_PORT", "70000"),
        ("QDRANT_HOST", ""),
        ("QDRANT_COLLECTION", ""),
        ("EMBEDDING_MODEL_NAME", ""),
        ("EMBEDDING_DIMENSIONS", "0"),
        ("REDIS_URL", ""),
        ("AWS_REGION", ""),
        ("BEDROCK_MODEL_ID_REASONING", ""),
    ],
)
def test_nonsense_values_are_rejected_at_startup(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """A bad value should fail loudly here, not as a confusing error mid-query."""
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_settings_are_read_once(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_HOST", "first")
    first = get_settings()

    monkeypatch.setenv("QDRANT_HOST", "second")

    assert get_settings() is first
    assert get_settings().qdrant_host == "first"


def test_bedrock_and_redis_come_from_the_environment(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three names TASK-012 started reading already existed in .env.example."""
    monkeypatch.setenv("REDIS_URL", "redis://cache.internal:6379/2")
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("BEDROCK_MODEL_ID_REASONING", "anthropic.some-other-sonnet")

    settings = Settings()

    assert settings.redis_url == "redis://cache.internal:6379/2"
    assert settings.aws_region == "us-east-2"
    assert settings.bedrock_model_id_reasoning == "anthropic.some-other-sonnet"


def test_the_reasoning_model_default_is_sonnet_not_haiku() -> None:
    """CLAUDE.md's Bedrock table puts policy analysis on Sonnet; a fast-model
    default here would be a silent downgrade of the one call site that reasons."""
    assert "sonnet" in DEFAULT_BEDROCK_MODEL_ID_REASONING

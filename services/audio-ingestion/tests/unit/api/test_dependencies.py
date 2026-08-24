"""The dependency wiring, including the seam the tests themselves rely on."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.api import dependencies
from src.config import get_settings


@pytest.fixture(autouse=True)
def clean_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    get_settings.cache_clear()
    dependencies._redis_client.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", "dependencies-test-signing-key-32by")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    yield None
    dependencies._redis_client.cache_clear()
    get_settings.cache_clear()


async def test_the_redis_client_is_built_once_and_reused() -> None:
    """A client per request would open a connection pool per request."""
    assert await dependencies.get_redis() is await dependencies.get_redis()


async def test_closing_releases_the_client_so_a_later_start_rebuilds_it() -> None:
    first = await dependencies.get_redis()

    await dependencies.close_redis()

    assert await dependencies.get_redis() is not first


async def test_settings_are_exposed_as_a_dependency() -> None:
    """The route takes settings by injection so a test can supply its own."""
    assert (await dependencies.get_app_settings()).redis_url == "redis://localhost:6379/0"


async def test_the_factory_opens_a_transcribe_medical_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one place the route is wired to AWS, and the seam tests replace.

    The dependency returns a factory rather than a stream, so nothing is opened
    until a connection is actually accepted — which is what makes "a refused
    token opens no Transcribe stream" enforceable rather than merely intended.
    """
    opened: list[object] = []

    async def fake_open(settings: object) -> str:
        opened.append(settings)
        return "a-stream"

    monkeypatch.setattr(dependencies, "open_medical_stream", fake_open)

    factory = await dependencies.get_transcription_factory()
    assert opened == []  # resolving the dependency opens nothing

    assert await factory() == "a-stream"
    assert len(opened) == 1

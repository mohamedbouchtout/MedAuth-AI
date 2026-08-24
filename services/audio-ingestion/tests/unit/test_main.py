"""The application factory and what it wires up."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from src.api import dependencies
from src.main import create_app

pytestmark = pytest.mark.usefixtures("signing_key")


@pytest.fixture
def signing_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", "main-unit-test-signing-key-32bytes")
    yield None
    get_settings.cache_clear()


def test_the_factory_returns_an_isolated_app() -> None:
    """Tests override dependencies on their own instance, never on a shared one."""
    assert create_app() is not create_app()


def test_both_surfaces_are_mounted() -> None:
    """Resolved by route name rather than by walking ``app.routes``.

    FastAPI wraps an included router instead of flattening its routes into the
    application's list, so a path search over ``app.routes`` finds only the
    docs endpoints. ``url_path_for`` is the supported way to ask.
    """
    app = create_app()

    assert app.url_path_for("health") == "/health"
    assert app.url_path_for("audio_stream", session_id="abc") == "/ws/audio/abc"


def test_the_websocket_is_absent_from_the_openapi_document() -> None:
    """Not an omission — OpenAPI 3.1 has no way to describe a WebSocket route.

    ``docs/api/audio-ingestion.yaml`` documents it in prose for that reason.
    """
    assert sorted(create_app().openapi()["paths"]) == ["/health"]


def test_the_shared_error_handlers_are_installed() -> None:
    """A 404 comes back in the envelope, not FastAPI's bare ``detail`` shape."""
    with TestClient(create_app()) as client:
        body = client.get("/no-such-route").json()

    assert body["data"] is None
    assert body["error"]["code"]


def test_nothing_connects_at_startup() -> None:
    """The service comes up even when Redis is briefly unreachable.

    Redis connects on first command, and a transcription stream belongs to a
    connection rather than to the process, so there is nothing to open here.
    """
    dependencies._redis_client.cache_clear()

    with TestClient(create_app()):
        assert dependencies._redis_client.cache_info().currsize == 0


async def test_closing_redis_when_it_was_never_opened_is_a_no_op() -> None:
    """Shutdown after a start that never served a request must not raise."""
    dependencies._redis_client.cache_clear()

    await dependencies.close_redis()

    assert dependencies._redis_client.cache_info().currsize == 0

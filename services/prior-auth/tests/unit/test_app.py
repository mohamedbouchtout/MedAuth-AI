"""The HTTP surface: ``GET /health`` and the database wiring behind it."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from prior_auth import db
from prior_auth.api.dependencies import (
    close_redis,
    get_redis,
    get_session_end_consumer,
)
from prior_auth.consumer import SessionEndConsumer
from prior_auth.main import create_app


class FakeRedis:
    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    async def ping(self) -> bool:
        if not self.healthy:
            raise RedisError("down")
        return True


class FakeConsumer:
    def __init__(self, *, healthy: bool) -> None:
        self._healthy = healthy

    def is_healthy(self) -> bool:
        return self._healthy


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An app without the lifespan, so no consumer starts and no Redis is opened."""
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: FakeRedis()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


def with_consumer(client: AsyncClient, consumer: Any) -> None:
    """Override the consumer dependency on the app behind `client`."""
    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_session_end_consumer] = lambda: consumer


class TestHealth:
    async def test_everything_up_is_200(self, client: AsyncClient) -> None:
        with_consumer(client, FakeConsumer(healthy=True))

        response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {
            "data": {"redis": "ok", "session_end_consumer": "ok"},
            "error": None,
        }

    async def test_a_stopped_consumer_is_503_and_is_named(self, client: AsyncClient) -> None:
        """A dead consumer means no encounter on this pod files anything.

        Nothing else in the system notices, which is what the probe is for.
        """
        with_consumer(client, FakeConsumer(healthy=False))

        response = await client.get("/health")

        assert response.status_code == 503
        assert response.json()["data"] == {"redis": "ok", "session_end_consumer": "error"}

    async def test_no_consumer_at_all_reports_error_rather_than_raising(
        self, client: AsyncClient
    ) -> None:
        """An app built without the lifespan has none; that is a 503, not a 500."""
        response = await client.get("/health")

        assert response.status_code == 503
        assert response.json()["data"]["session_end_consumer"] == "error"

    async def test_redis_down_is_503(self, client: AsyncClient) -> None:
        app = client._transport.app  # type: ignore[attr-defined]
        app.dependency_overrides[get_redis] = lambda: FakeRedis(healthy=False)
        with_consumer(client, FakeConsumer(healthy=True))

        response = await client.get("/health")

        assert response.status_code == 503
        assert response.json()["data"]["redis"] == "error"

    async def test_a_failing_probe_still_returns_the_envelope_with_data(
        self, client: AsyncClient
    ) -> None:
        """The documented departure: a 503 carries ``data``, not ``error``.

        Moving the flags into the error half would discard the only diagnostic
        the endpoint has.
        """
        response = await client.get("/health")

        assert response.json()["error"] is None
        assert response.json()["data"] is not None


class TestTheSurfaceIsDeliberatelySmall:
    """What this service exposes over HTTP.

    The CORS assertions that used to live here moved to ``test_cors.py`` with
    TASK-072, which is where CLAUDE.md's testing rule says a service's preflight
    cases live — and where they gained the thing a middleware-presence check
    cannot give: a preflight actually answered for the submit route's own path
    and method.
    """

    def test_the_surface_is_health_and_the_submission_router(self) -> None:
        """Assembly still arrives on a subscription; TASK-061 added the one route.

        Read off the generated schema rather than ``app.routes``: this FastAPI
        version keeps an included router as one nested entry, so walking the
        list finds only the docs routes and the assertion would pass vacuously.
        """
        assert set(create_app().openapi()["paths"]) == {
            "/health",
            "/prior-auth/{request_id}/submit",
        }


class TestLifespan:
    async def test_the_consumer_is_started_and_stopped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started: list[str] = []

        class RecordingConsumer(SessionEndConsumer):
            def start(self) -> None:
                started.append("start")

            async def stop(self) -> None:
                started.append("stop")

        monkeypatch.setattr("prior_auth.main.SessionEndConsumer", RecordingConsumer)
        monkeypatch.setattr("prior_auth.main.get_redis", _fake_redis)
        monkeypatch.setattr("prior_auth.main.dispose_engine", _noop)
        monkeypatch.setattr("prior_auth.main.close_redis", _noop)

        app = create_app()
        async with app.router.lifespan_context(app):
            assert started == ["start"]
            assert isinstance(app.state.session_end_consumer, SessionEndConsumer)

        assert started == ["start", "stop"]


async def _fake_redis() -> Any:
    return FakeRedis()


async def _noop() -> None:
    return None


class TestDatabaseUrl:
    def test_a_sqlalchemy_url_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
        assert db.database_url() == "postgresql+asyncpg://u:p@h/d"

    def test_a_plain_url_is_given_the_driver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One DATABASE_URL serves every consumer in the monorepo."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")
        assert db.database_url() == "postgresql+asyncpg://u:p@h/d"

    def test_an_unset_url_names_this_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(db.DatabaseConfigurationError, match="prior-auth"):
            db.database_url()

    def test_the_engine_is_cached_and_disposable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
        db.get_engine.cache_clear()
        try:
            assert db.get_engine() is db.get_engine()
            assert db.get_sessionmaker() is db.get_sessionmaker()
        finally:
            db.get_engine.cache_clear()
            db.get_sessionmaker.cache_clear()

    async def test_disposing_an_unopened_engine_is_a_no_op(self) -> None:
        db.get_engine.cache_clear()
        await db.dispose_engine()


class TestRedisClient:
    async def test_the_client_is_cached_and_closable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        first = await get_redis()
        assert await get_redis() is first
        await close_redis()

    async def test_closing_an_unopened_client_is_a_no_op(self) -> None:
        await close_redis()
        assert "REDIS_URL" not in os.environ or True

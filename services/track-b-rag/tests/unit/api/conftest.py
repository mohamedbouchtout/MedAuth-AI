"""Fakes that let the routes be exercised without Qdrant, Redis or the model.

The integration suite covers the same routes against real containers and the
real weights; these fakes exist so the contract — envelope shape, status codes,
which flag goes with which dependency — stays testable on a machine with none
of them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from track_b_rag import embeddings, vector_store
from track_b_rag.api.dependencies import get_qdrant, get_redis
from track_b_rag.config import get_settings
from track_b_rag.main import create_app


class FakeQdrant:
    """A client that answers, or doesn't."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    def get_collections(self) -> object:
        if not self.healthy:
            raise ConnectionError("qdrant is down")
        return object()


class FakeRedis:
    """An in-memory stand-in for the cache, with a switch for being down.

    Keeps what was written and with what TTL, because "the answer was cached"
    and "the fallback was not" are both assertions about this object rather than
    about the response body.
    """

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.store: dict[str, str] = {}
        self.expiries: dict[str, int | None] = {}

    async def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("redis is down")
        return True

    async def get(self, key: str) -> bytes | None:
        if not self.healthy:
            raise RedisError("redis is down")
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        if not self.healthy:
            raise RedisError("redis is down")
        self.store[key] = value
        self.expiries[key] = ex
        return True


@pytest.fixture(autouse=True)
def clean_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_qdrant() -> FakeQdrant:
    return FakeQdrant()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def embedding_health(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Let a test declare whether the embedding model is loadable.

    Defaults to healthy so a test that only cares about Qdrant does not have to
    say so, and never touches the real weights either way.
    """

    def set_healthy(healthy: bool) -> None:
        monkeypatch.setattr(embeddings, "check_health", lambda: healthy)

    set_healthy(True)
    return set_healthy


@pytest_asyncio.fixture
async def client(
    fake_qdrant: FakeQdrant,
    fake_redis: FakeRedis,
    embedding_health: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app with every dependency replaced.

    ``create_app``'s lifespan is not entered — ASGITransport does not run it —
    so nothing here reaches for a real Qdrant on startup. The startup path has
    its own tests in ``test_main.py``.
    """
    monkeypatch.setattr(vector_store, "get_client", lambda: fake_qdrant)
    app = create_app()
    app.dependency_overrides[get_qdrant] = lambda: fake_qdrant
    app.dependency_overrides[get_redis] = lambda: fake_redis
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://track-b-rag") as http:
        yield http

"""Fakes that let the health route be exercised without Qdrant or the model.

The integration suite covers the same route against a real container and the
real weights; these fakes exist so the contract — envelope shape, status codes,
which flag goes with which dependency — stays testable on a machine with
neither.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from track_b_rag import embeddings, vector_store
from track_b_rag.api.dependencies import get_qdrant
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


@pytest.fixture(autouse=True)
def clean_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_qdrant() -> FakeQdrant:
    return FakeQdrant()


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
    embedding_health: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app with both dependencies replaced.

    ``create_app``'s lifespan is not entered — ASGITransport does not run it —
    so nothing here reaches for a real Qdrant on startup. The startup path has
    its own tests in ``test_main.py``.
    """
    monkeypatch.setattr(vector_store, "get_client", lambda: fake_qdrant)
    app = create_app()
    app.dependency_overrides[get_qdrant] = lambda: fake_qdrant
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://track-b-rag") as http:
        yield http

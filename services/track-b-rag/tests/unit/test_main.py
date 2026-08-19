"""Application startup: ensure the collection, tolerate a Qdrant that is not up yet."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from track_b_rag import main, vector_store
from track_b_rag.config import get_settings
from track_b_rag.main import create_app, initialize_vector_store, lifespan


class FakeClient:
    """Records what startup asked for, or fails on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.ensured: list[tuple[str, int]] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def clean_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient()
    monkeypatch.setattr(main, "get_client", lambda: client)
    monkeypatch.setattr(main, "close_client", lambda: None)

    def ensure(passed: Any, name: str, size: int) -> bool:
        if client.fail:
            raise ConnectionError("qdrant is not up")
        client.ensured.append((name, size))
        return True

    monkeypatch.setattr(main, "ensure_collection", ensure)
    return client


async def test_startup_ensures_the_configured_collection(fake_client: FakeClient) -> None:
    assert await initialize_vector_store() is True
    assert fake_client.ensured == [("insurance_policies", 1024)]


async def test_startup_uses_the_configured_dimensions(
    fake_client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1024 comes from EMBEDDING_DIMENSIONS, not a literal at the call site."""
    monkeypatch.setenv("QDRANT_COLLECTION", "policies_staging")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    get_settings.cache_clear()

    await initialize_vector_store()

    assert fake_client.ensured == [("policies_staging", 768)]


async def test_an_unreachable_qdrant_does_not_stop_the_service(
    fake_client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A crash loop on a slow container hides the cause; 503 from /health shows it."""
    fake_client.fail = True

    assert await initialize_vector_store() is False
    assert "Could not reach Qdrant" in caplog.text


async def test_the_app_starts_and_shuts_down(fake_client: FakeClient) -> None:
    async with lifespan(create_app()):
        pass

    assert fake_client.ensured == [("insurance_policies", 1024)]


async def test_shutdown_releases_the_client_and_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []
    monkeypatch.setattr(main, "ensure_collection", lambda *_: True)
    monkeypatch.setattr(main, "get_client", lambda: FakeClient())
    monkeypatch.setattr(main, "close_client", lambda: released.append("qdrant"))
    monkeypatch.setattr(main, "reset_embedder", lambda: released.append("embedder"))

    async with lifespan(create_app()):
        pass

    assert released == ["qdrant", "embedder"]


def test_the_app_never_loads_the_model_at_import_time() -> None:
    """Startup must not pay for 1.3 GB of weights before the port opens."""
    create_app()

    from track_b_rag.embeddings import get_embedder

    assert get_embedder.cache_info().currsize == 0


def test_health_is_the_only_route_so_far() -> None:
    """Read from the generated spec: FastAPI nests included routers in app.routes."""
    paths = set(create_app().openapi()["paths"])

    assert paths == {"/health"}


def test_vector_store_module_is_the_one_startup_calls() -> None:
    """Guards against main.py growing its own copy of the get-or-create logic."""
    assert main.ensure_collection is vector_store.ensure_collection

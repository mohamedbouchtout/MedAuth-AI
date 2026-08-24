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
        self.indexed: list[str] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeConsumer:
    """Stands in for the transcript consumer so startup opens no connection."""

    instances: list[FakeConsumer] = []

    def __init__(self, redis: Any, **_: Any) -> None:
        self.redis = redis
        self.started = 0
        self.stopped = 0
        FakeConsumer.instances.append(self)

    def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


@pytest.fixture
def fake_consumer(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeConsumer]]:
    """Replace the consumer for every test that enters the lifespan.

    Without this, startup would launch the real read loop and it would try to
    reach Redis on localhost — a unit test must not depend on a broker being up.
    """
    FakeConsumer.instances = []
    monkeypatch.setattr(main, "TranscriptConsumer", FakeConsumer)
    monkeypatch.setattr(main, "get_redis_client", lambda: object())
    yield FakeConsumer
    FakeConsumer.instances = []


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

    def ensure_indexes(passed: Any, name: str) -> tuple[str, ...]:
        client.indexed.append(name)
        return ()

    monkeypatch.setattr(main, "ensure_payload_indexes", ensure_indexes)
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


async def test_startup_ensures_the_payload_indexes_too(fake_client: FakeClient) -> None:
    """TASK-011's indexes are get-or-create at startup, same as the collection."""
    await initialize_vector_store()

    assert fake_client.indexed == ["insurance_policies"]


async def test_indexes_are_ensured_after_the_collection_not_before(
    fake_client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An index cannot be created on a collection that does not exist yet."""
    order: list[str] = []
    monkeypatch.setattr(main, "ensure_collection", lambda *_: order.append("collection"))
    monkeypatch.setattr(main, "ensure_payload_indexes", lambda *_: order.append("indexes"))

    await initialize_vector_store()

    assert order == ["collection", "indexes"]


async def test_an_unreachable_qdrant_does_not_stop_the_service(
    fake_client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A crash loop on a slow container hides the cause; 503 from /health shows it."""
    fake_client.fail = True

    assert await initialize_vector_store() is False
    assert "Could not reach Qdrant" in caplog.text


async def test_the_app_starts_and_shuts_down(
    fake_client: FakeClient, fake_consumer: type[FakeConsumer]
) -> None:
    async with lifespan(create_app()):
        pass

    assert fake_client.ensured == [("insurance_policies", 1024)]


async def test_startup_starts_the_transcript_consumer(
    fake_client: FakeClient, fake_consumer: type[FakeConsumer]
) -> None:
    """Nothing else creates it, and without it no encounter raises a nudge."""
    app = create_app()

    async with lifespan(app):
        (consumer,) = fake_consumer.instances
        assert consumer.started == 1
        assert app.state.transcript_consumer is consumer


async def test_shutdown_stops_the_consumer_before_closing_redis(
    fake_client: FakeClient, fake_consumer: type[FakeConsumer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """It holds a pub/sub connection on that client; the order is not incidental."""
    order: list[str] = []

    class Recording(FakeConsumer):
        async def stop(self) -> None:
            order.append("consumer")

    async def close_redis() -> None:
        order.append("redis")

    monkeypatch.setattr(main, "TranscriptConsumer", Recording)
    monkeypatch.setattr(main, "close_redis", close_redis)

    async with lifespan(create_app()):
        pass

    assert order == ["consumer", "redis"]


async def test_shutdown_releases_the_client_and_the_model(
    fake_consumer: type[FakeConsumer], monkeypatch: pytest.MonkeyPatch
) -> None:
    released: list[str] = []
    monkeypatch.setattr(main, "ensure_collection", lambda *_: True)
    monkeypatch.setattr(main, "ensure_payload_indexes", lambda *_: ())
    monkeypatch.setattr(main, "get_client", lambda: FakeClient())
    monkeypatch.setattr(main, "close_client", lambda: released.append("qdrant"))
    monkeypatch.setattr(main, "reset_embedder", lambda: released.append("embedder"))
    monkeypatch.setattr(main, "reset_bedrock_clients", lambda: released.append("bedrock"))

    async def close_redis() -> None:
        released.append("redis")

    async def dispose() -> None:
        released.append("engine")

    monkeypatch.setattr(main, "close_redis", close_redis)
    monkeypatch.setattr(main, "dispose_engine", dispose)

    async with lifespan(create_app()):
        pass

    assert released == ["qdrant", "embedder", "bedrock", "redis", "engine"]


def test_the_app_never_loads_the_model_at_import_time() -> None:
    """Startup must not pay for 1.3 GB of weights before the port opens."""
    create_app()

    from track_b_rag.embeddings import get_embedder

    assert get_embedder.cache_info().currsize == 0


def test_the_app_exposes_health_ingest_and_query() -> None:
    """Read from the generated spec: FastAPI nests included routers in app.routes."""
    paths = set(create_app().openapi()["paths"])

    assert paths == {"/health", "/policies/ingest", "/policies/query"}


def test_the_app_never_builds_a_bedrock_client_at_startup() -> None:
    """Same reasoning as the embedding model: a rollout must not wait on AWS.

    Building the client resolves credentials and loads service metadata, and a
    replica that never answers a query should never pay for either.
    """
    create_app()

    from track_b_rag.bedrock import get_reasoning_model, get_runtime_client

    assert get_runtime_client.cache_info().currsize == 0
    assert get_reasoning_model.cache_info().currsize == 0


def test_vector_store_module_is_the_one_startup_calls() -> None:
    """Guards against main.py growing its own copy of the get-or-create logic."""
    assert main.ensure_collection is vector_store.ensure_collection
    assert main.ensure_payload_indexes is vector_store.ensure_payload_indexes

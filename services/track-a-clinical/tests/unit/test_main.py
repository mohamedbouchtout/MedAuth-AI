"""The application factory and its shutdown lifespan."""

from __future__ import annotations

import pytest

from track_a_clinical import bedrock, consumer, db, main
from track_a_clinical.api import dependencies
from track_a_clinical.main import create_app


def test_app_exposes_the_session_note_and_prior_auth_routes() -> None:
    paths = create_app().openapi()["paths"]

    assert set(paths) == {
        "/health",
        "/sessions/start",
        "/sessions/{session_id}/end",
        "/sessions/{session_id}/token",
        "/notes/{session_id}",
        "/notes/{session_id}/ehr-reference",
        "/prior-auth/{request_id}",
        "/prior-auth/{request_id}/submission",
        "/providers/resolve",
    }
    assert set(paths["/sessions/start"]) == {"post"}
    # Read and edit share one path, which is why this asserts on the methods:
    # a router registered with only one of them would still pass the set above.
    assert set(paths["/notes/{session_id}"]) == {"get", "patch"}
    # Same again for the EHR-linkage sub-resource: a GET the write-back reads its
    # identifiers from, and the PATCH that records what it filed (TASK-053).
    assert set(paths["/notes/{session_id}/ehr-reference"]) == {"get", "patch"}
    # The prior-auth pair is two paths rather than one for a reason worth
    # keeping: the read is a whole request, and the PATCH is a sub-resource that
    # records a submission, so a client cannot reach the write by guessing.
    assert set(paths["/prior-auth/{request_id}"]) == {"get"}
    assert set(paths["/prior-auth/{request_id}/submission"]) == {"patch"}


def test_each_app_is_independent() -> None:
    """The factory exists so a test can override dependencies without leaking."""
    first, second = create_app(), create_app()
    first.dependency_overrides[dependencies.get_redis] = lambda: None

    assert second.dependency_overrides == {}


def test_start_documents_the_created_status() -> None:
    """201, not 200 — the call creates an encounter. Clients read this from the spec."""
    operation = create_app().openapi()["paths"]["/sessions/start"]["post"]

    assert set(operation["responses"]) >= {"201"}


class _FakeConsumer:
    """Records the lifecycle calls without touching Redis."""

    def __init__(self, redis: object) -> None:
        self.redis = redis
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")


@pytest.fixture
def stubbed_lifespan(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace everything the lifespan reaches for, returning the call order."""
    order: list[str] = []

    async def fake_redis() -> object:
        return object()

    async def record_engine() -> None:
        order.append("engine")

    async def record_redis() -> None:
        order.append("redis")

    def record_reset() -> None:
        order.append("bedrock")

    def build(redis: object) -> _FakeConsumer:
        consumer = _FakeConsumer(redis)
        # Wrap so the consumer's own calls land in the same ordered list.
        original_start, original_stop = consumer.start, consumer.stop

        def start() -> None:
            original_start()
            order.append("consumer-start")

        async def stop() -> None:
            await original_stop()
            order.append("consumer-stop")

        consumer.start = start  # type: ignore[method-assign]
        consumer.stop = stop  # type: ignore[method-assign]
        return consumer

    monkeypatch.setattr(main, "get_redis", fake_redis)
    monkeypatch.setattr(main, "TranscriptConsumer", build)
    monkeypatch.setattr(main, "dispose_engine", record_engine)
    monkeypatch.setattr(main, "close_redis", record_redis)
    monkeypatch.setattr(main, "reset_clients", record_reset)
    return order


async def test_lifespan_starts_the_consumer_and_publishes_it(
    stubbed_lifespan: list[str],
) -> None:
    """GET /health reaches the consumer through app.state, so it has to be there."""
    app = create_app()

    async with main.lifespan(app):
        assert stubbed_lifespan == ["consumer-start"]
        assert app.state.transcript_consumer is not None


async def test_lifespan_stops_the_consumer_before_closing_redis(
    stubbed_lifespan: list[str],
) -> None:
    """The consumer holds a pub/sub connection on that client, so order matters."""
    async with main.lifespan(create_app()):
        pass

    assert stubbed_lifespan.index("consumer-stop") < stubbed_lifespan.index("redis")
    assert stubbed_lifespan == ["consumer-start", "consumer-stop", "engine", "redis", "bedrock"]


def test_lifespan_uses_the_real_release_hooks() -> None:
    """Guards the monkeypatching above against renamed hooks."""
    assert main.dispose_engine is db.dispose_engine
    assert main.close_redis is dependencies.close_redis
    assert main.get_redis is dependencies.get_redis
    assert main.reset_clients is bedrock.reset_clients
    assert main.TranscriptConsumer is consumer.TranscriptConsumer

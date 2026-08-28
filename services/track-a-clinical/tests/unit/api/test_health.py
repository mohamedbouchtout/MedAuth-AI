"""``GET /health`` — what it reports, and the two conventions it departs from.

The consumer flag is the reason this endpoint exists at all: a stopped consumer
means no encounter on this pod produces a note, and nothing else in the system
would say so.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from track_a_clinical.api.dependencies import get_redis, get_transcript_consumer
from track_a_clinical.consumer import TranscriptConsumer
from track_a_clinical.main import create_app


class PingableRedis:
    """A Redis stand-in that answers, or refuses to."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def ping(self) -> bool:
        if self.fail:
            raise RedisError("broker unreachable")
        return True


def make_consumer(*, healthy: bool) -> TranscriptConsumer:
    """Return a real consumer object reporting the requested liveness."""
    consumer = TranscriptConsumer(redis=None)  # type: ignore[arg-type]
    consumer.is_healthy = lambda: healthy  # type: ignore[method-assign]
    return consumer


@pytest_asyncio.fixture
async def probe() -> AsyncIterator[Any]:
    """Return a callable that probes /health with the given dependency states."""
    apps: list[Any] = []

    async def run(*, redis_ok: bool = True, consumer: TranscriptConsumer | None = None) -> Any:
        app = create_app()
        app.dependency_overrides[get_redis] = lambda: PingableRedis(fail=not redis_ok)
        app.dependency_overrides[get_transcript_consumer] = lambda: consumer
        apps.append(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://track-a") as http:
            return await http.get("/health")

    yield run


async def test_everything_up_is_two_hundred(probe: Any) -> None:
    response = await probe(consumer=make_consumer(healthy=True))

    assert response.status_code == 200
    assert response.json() == {
        "data": {"redis": "ok", "transcript_consumer": "ok"},
        "error": None,
    }


async def test_a_stopped_consumer_is_five_oh_three(probe: Any) -> None:
    """The failure this endpoint was added for: silent, and otherwise invisible."""
    response = await probe(consumer=make_consumer(healthy=False))

    assert response.status_code == 503
    assert response.json()["data"]["transcript_consumer"] == "error"


async def test_no_consumer_at_all_is_five_oh_three(probe: Any) -> None:
    """An app built without the lifespan has none, and that is not healthy."""
    response = await probe(consumer=None)

    assert response.status_code == 503
    assert response.json()["data"]["transcript_consumer"] == "error"


async def test_an_unreachable_redis_is_five_oh_three(probe: Any) -> None:
    response = await probe(redis_ok=False, consumer=make_consumer(healthy=True))

    assert response.status_code == 503
    assert response.json()["data"]["redis"] == "error"


async def test_a_failing_probe_still_names_the_healthy_dependencies(probe: Any) -> None:
    """A 503 that only says "unhealthy" is not diagnosable from the response."""
    response = await probe(redis_ok=False, consumer=make_consumer(healthy=True))

    assert response.json()["data"] == {"redis": "error", "transcript_consumer": "ok"}


async def test_a_five_oh_three_carries_data_not_error(probe: Any) -> None:
    """The request succeeded; the answer is "unhealthy". CLAUDE.md documents this."""
    body = (await probe(redis_ok=False, consumer=make_consumer(healthy=True))).json()

    assert body["error"] is None
    assert body["data"] is not None


async def test_health_writes_no_audit_row(probe: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe touches no PHI, and auditing one on its polling interval is noise."""
    calls: list[Any] = []

    from track_a_clinical import audit

    async def record(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(audit, "audit_encounter_access", record)
    monkeypatch.setattr(audit, "audit_note_write", record)

    await probe(consumer=make_consumer(healthy=True))

    assert calls == []

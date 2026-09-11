"""The transcript socket against a real Redis (TASK-041d).

Skipped when REDIS_URL is unset, like the other integration suites, so the unit
tests still run on a machine with no backing services. In CI Redis comes up from
docker-compose.yml and these always run.

What only this suite can prove: that a real publisher on the canonical
``transcription:{session_id}`` channel reaches a real subscriber here, through
redis-py's actual pub/sub delivery rather than through a queue a test filled in.
The unit suite hands messages to the relay directly, which cannot catch a wrong
channel name, a subscription that never completed, or a message type the filter
rejects — and a wrong channel name is the single most likely defect in a second
stream sharing one lifecycle with the first.

The two conventions this suite inherits from the nudge one, both load-bearing:
the assertion happens while the socket is still open, because the synchronous
``TestClient`` cancels the application task as soon as the context manager exits;
and the publish is retried, because ``subscribe`` returning is not the same as
the server having registered the subscription for delivery, and a message
published into that gap is dropped rather than queued — pub/sub has no backlog.
A sleep long enough to "usually" work is how a test becomes flaky on a loaded
runner.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import replace

import pytest
import redis as sync_redis
from fastapi.testclient import TestClient

from src.api import websocket as websocket_module
from src.config import get_settings
from src.main import create_app
from tests.unit.api.conftest import (
    SIGNING_KEY,
    RecordedAudit,
    header_carrier,
    mint_token,
    subprotocol_carrier,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("REDIS_URL"),
        reason="REDIS_URL is not set — this suite needs a real Redis",
    ),
]

#: How many times to publish before giving up. Each attempt is followed by a read
#: with the socket's own timeout, so this is not a busy loop.
PUBLISH_ATTEMPTS = 20

SEGMENT = json.dumps(
    {
        "session_id": "0b7f0f3e-2f1a-4a0e-9a3a-2c9a9a6a1c11",
        "result_id": "a1b2c3d4-0000-4000-8000-000000000001",
        "text": "Patient reports right knee pain for about six weeks.",
        "is_partial": False,
        "start_time": 12.34,
        "end_time": 16.78,
    }
)


@pytest.fixture
def redis_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = os.environ["REDIS_URL"]
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("REDIS_URL", url)
    yield url
    get_settings.cache_clear()


@pytest.fixture
def publisher(redis_url: str) -> Iterator[sync_redis.Redis]:
    client = sync_redis.Redis.from_url(redis_url)
    yield client
    client.close()


@pytest.fixture
def recorded_audit(monkeypatch: pytest.MonkeyPatch) -> RecordedAudit:
    """The audit write needs a database; this suite is about the bus.

    Patches the stream description rather than a module-level function, for the
    reason recorded in the unit conftest: the route reaches its audit function
    through the ``RelayedStream`` it names, so patching the function on the
    module would replace a reference nothing reads.
    """
    recorder = RecordedAudit()
    monkeypatch.setattr(
        websocket_module,
        "TRANSCRIPT_STREAM",
        replace(websocket_module.TRANSCRIPT_STREAM, audit=recorder),
    )
    return recorder


@pytest.fixture
def client(redis_url: str, recorded_audit: RecordedAudit) -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def relay_one(socket: object, publisher: sync_redis.Redis, channel: str, payload: str) -> str:
    """Publish until a subscriber picks it up, and return what arrived."""
    for attempt in range(PUBLISH_ATTEMPTS):
        delivered = publisher.publish(channel, payload)
        if delivered:
            return socket.receive_text()  # type: ignore[attr-defined]
        if attempt == PUBLISH_ATTEMPTS - 1:  # pragma: no cover - defensive
            pytest.fail(f"no subscriber on {channel} after {PUBLISH_ATTEMPTS} publishes")
    raise AssertionError("unreachable")


def test_a_segment_published_to_redis_reaches_the_client(
    client: TestClient, publisher: sync_redis.Redis
) -> None:
    """TASK-041d's acceptance criterion, over a real bus."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        f"/ws/transcript/{session_id}", headers=header_carrier(token)
    ) as socket:
        received = relay_one(socket, publisher, f"transcription:{session_id}", SEGMENT)

    assert received == SEGMENT


def test_the_segment_survives_the_bus_byte_for_byte(
    client: TestClient, publisher: sync_redis.Redis
) -> None:
    """Redis carries bytes; the relay must not normalise them on the way out."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)
    odd_spacing = '{"text":"hello",   "is_partial":false}\n'

    with client.websocket_connect(
        f"/ws/transcript/{session_id}", headers=header_carrier(token)
    ) as socket:
        received = relay_one(socket, publisher, f"transcription:{session_id}", odd_spacing)

    assert received == odd_spacing


def test_the_browser_carrier_works_over_a_real_bus(
    client: TestClient, publisher: sync_redis.Redis
) -> None:
    """apps/web has no other way in, so this path gets the same coverage."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        f"/ws/transcript/{session_id}", subprotocols=subprotocol_carrier(token)
    ) as socket:
        received = relay_one(socket, publisher, f"transcription:{session_id}", SEGMENT)

    assert received == SEGMENT


def test_a_nudge_on_the_other_channel_does_not_arrive(
    client: TestClient, publisher: sync_redis.Redis
) -> None:
    """The two streams share a lifecycle and must not share a subscription.

    This is the case the unit suite cannot make: it publishes a real message to
    the *nudge* channel for the very same session, which a transcript socket that
    subscribed to the wrong template would happily relay. Only the segment may
    arrive.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        f"/ws/transcript/{session_id}", headers=header_carrier(token)
    ) as socket:
        publisher.publish(f"nudges:{session_id}", '{"type":"PAYER_RULE_ALERT"}')
        received = relay_one(socket, publisher, f"transcription:{session_id}", SEGMENT)

    assert received == SEGMENT


def test_another_encounters_transcript_does_not_arrive(
    client: TestClient, publisher: sync_redis.Redis
) -> None:
    """The subscription is one channel by name, never a pattern.

    A wildcard across the channel family carrying speech would hand one client
    every encounter in the clinic, which is the worst version of this mistake in
    the repository.
    """
    session_id = uuid.uuid4()
    other_session = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        f"/ws/transcript/{session_id}", headers=header_carrier(token)
    ) as socket:
        publisher.publish(f"transcription:{other_session}", '{"text":"another patient"}')
        received = relay_one(socket, publisher, f"transcription:{session_id}", SEGMENT)

    assert received == SEGMENT

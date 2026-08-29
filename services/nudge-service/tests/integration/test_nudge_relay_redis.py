"""The nudge socket against a real Redis.

Skipped when REDIS_URL is unset, like the other integration suites, so the unit
tests still run on a machine with no backing services. In CI Redis comes up from
docker-compose.yml and these always run.

What only this suite can prove: that a real publisher on the canonical channel
reaches a real subscriber here, through redis-py's actual pub/sub delivery rather
than through a queue a test filled in. The unit suite hands messages to the relay
directly, which cannot catch a wrong channel name, a subscription that never
completed, or a message type the filter rejects.

**Why the assertion happens while the socket is still open.** The synchronous
``TestClient`` cancels the application task as soon as the WebSocket context
manager exits — it closes the connection and cancels, without waiting for the
handler to finish. So the nudge is published and read back inside the context,
and the unsubscribe path is left to the unit suite, where the fake bus completes
without yielding.

**Why the publish is retried.** ``subscribe`` returning is not the same as the
server having registered the subscription for delivery, and a message published
into that gap is dropped by Redis rather than queued — pub/sub has no backlog. A
short retry is the honest fix; a sleep long enough to "usually" work is how a
test becomes flaky on a loaded CI runner.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

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

NUDGE = json.dumps(
    {
        "type": "PAYER_RULE_ALERT",
        "nudge_id": "3f2a1b0c-9d8e-4f7a-8b6c-5d4e3f2a1b0c",
        "procedure": "knee MRI",
        "cpt_code": "73721",
        "message": "Prior authorization required for knee MRI.",
        "missing_criteria": ["six weeks of conservative therapy"],
        "denial_risk": "high",
        "haptic": True,
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
    """The audit write needs a database; this suite is about the bus."""
    recorder = RecordedAudit()
    monkeypatch.setattr(websocket_module, "audit_nudge_stream", recorder)
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


def test_a_nudge_published_to_redis_reaches_the_client(
    client: TestClient, publisher: sync_redis.Redis
) -> None:
    """TASK-041's acceptance criterion, over a real bus."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        f"/ws/nudges/{session_id}", headers=header_carrier(token)
    ) as socket:
        received = relay_one(socket, publisher, f"nudges:{session_id}", NUDGE)

    assert received == NUDGE


def test_the_payload_survives_the_bus_byte_for_byte(
    client: TestClient, publisher: sync_redis.Redis
) -> None:
    """Redis carries bytes; the relay must not normalise them on the way out."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)
    odd_spacing = '{"b":2,   "a":1}\n'

    with client.websocket_connect(
        f"/ws/nudges/{session_id}", headers=header_carrier(token)
    ) as socket:
        received = relay_one(socket, publisher, f"nudges:{session_id}", odd_spacing)

    assert received == odd_spacing


def test_the_browser_carrier_works_over_a_real_bus(
    client: TestClient, publisher: sync_redis.Redis
) -> None:
    """apps/web has no other way in, so this path gets the same coverage."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        f"/ws/nudges/{session_id}", subprotocols=subprotocol_carrier(token)
    ) as socket:
        received = relay_one(socket, publisher, f"nudges:{session_id}", NUDGE)

    assert received == NUDGE


def test_another_encounters_nudge_does_not_arrive(
    client: TestClient, publisher: sync_redis.Redis
) -> None:
    """The subscription is one channel by name, never a pattern.

    Published to a different session's channel and then to this one: only the
    second may arrive, and it must arrive *second* rather than the first being
    silently reordered into it.
    """
    session_id = uuid.uuid4()
    other_session = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        f"/ws/nudges/{session_id}", headers=header_carrier(token)
    ) as socket:
        publisher.publish(f"nudges:{other_session}", '{"leaked":true}')
        received = relay_one(socket, publisher, f"nudges:{session_id}", NUDGE)

    assert received == NUDGE

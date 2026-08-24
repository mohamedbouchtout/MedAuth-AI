"""The audio WebSocket against a real Redis, with transcription still faked.

Skipped when REDIS_URL is unset, like the other integration suites, so the unit
tests still run on a machine with no backing services. In CI Redis comes up from
docker-compose.yml and these always run.

**Why transcription stays faked even here.** AWS Transcribe Medical has no local
emulator, and moto does not implement the HTTP/2 event-stream protocol the
streaming API uses — its `transcribe` support covers batch jobs only. A live
test would need real AWS credentials, which CI deliberately does not hold: every
AWS call in this repository is mocked. What that leaves unverified is stated
plainly in TASKS.md rather than papered over here, and the serialized-request
assertions in tests/unit/test_transcribe_medical.py are what guard the AWS side.

**Why every assertion happens while the socket is still open.** The synchronous
``TestClient`` cancels the application task as soon as the WebSocket context
manager exits — it closes the connection and cancels, without waiting for the
handler to finish. Anything the service does after the disconnect can therefore
be cancelled mid-flight, and opening a real Redis connection is slow enough to
lose that race every time. So these tests use a transcriber that emits
mid-stream (``wait_for_end=False``) and read from the bus before disconnecting.
The post-disconnect path is exercised by the unit suite, where the fake bus
completes without yielding.

What only this suite can prove: that a real subscriber on the canonical channel
receives what the route publishes, in the shape TASK-021 and TASK-030 will parse.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import pytest
import redis as sync_redis
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api import websocket as websocket_module
from src.api.dependencies import get_transcription_factory
from src.config import get_settings
from src.main import create_app
from src.transcription import TranscriptSegment
from tests.unit.api.conftest import (
    SIGNING_KEY,
    FakeTranscriptionStream,
    RecordingFactory,
    header_carrier,
    mint_token,
)
from tests.wav import chunks, pcm_payload, wav_bytes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("REDIS_URL"),
        reason="REDIS_URL is not set — this suite needs a real Redis",
    ),
]


@pytest.fixture
def subscriber() -> Iterator[sync_redis.client.PubSub]:
    """A real Redis subscriber, connected before anything is published.

    Redis pub/sub has no replay: a subscriber that arrives after the publish
    hears nothing. Subscribing first is what makes these tests meaningful rather
    than flaky.
    """
    client = sync_redis.Redis.from_url(os.environ["REDIS_URL"])
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    yield pubsub
    pubsub.close()
    client.close()


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, RecordingFactory]]:
    """The app with a real Redis client and a faked transcriber.

    The audit write is stubbed out: this suite is about the bus, and the audit
    row against a real PostgreSQL is track-a-clinical's integration suite's job.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)

    async def no_audit(**_fields: object) -> None:
        return None

    monkeypatch.setattr(websocket_module, "audit_audio_stream", no_audit)

    factory = RecordingFactory(FakeTranscriptionStream(wait_for_end=False))
    app = create_app()
    app.dependency_overrides[get_transcription_factory] = lambda: factory
    with TestClient(app) as client:
        yield client, factory

    get_settings.cache_clear()


def read_published(
    pubsub: sync_redis.client.PubSub,
    *,
    expected: int,
    attempts: int = 20,
) -> list[dict[str, object]]:
    """Collect up to ``expected`` messages, giving up after ``attempts`` polls.

    ``attempts`` is what makes a negative assertion cost two seconds instead of
    twenty: a test proving nothing was published still has to wait long enough
    for something to have arrived if it were going to.
    """
    received: list[dict[str, object]] = []
    for _ in range(attempts):
        if len(received) >= expected:
            break
        message = pubsub.get_message(timeout=0.5)
        if message and message["type"] == "message":
            received.append(json.loads(message["data"]))
    return received


def test_ten_seconds_of_audio_reaches_a_real_subscriber(
    app_client: tuple[TestClient, RecordingFactory],
    subscriber: sync_redis.client.PubSub,
) -> None:
    """TASK-020's first acceptance test, against the real bus.

    The unit version proves the route publishes. This proves a separate process
    subscribed to the canonical channel actually receives it, in a payload it can
    parse without importing anything from this service.
    """
    client, factory = app_client
    session_id = uuid.uuid4()
    subscriber.subscribe(f"transcription:{session_id}")

    factory.stream.script(
        TranscriptSegment(
            result_id="r1",
            text="Let's get an MRI of that left knee.",
            is_partial=False,
            start_time=0.5,
            end_time=3.25,
        )
    )
    audio = pcm_payload(wav_bytes(seconds=10))

    with client.websocket_connect(
        f"/ws/audio/{session_id}",
        headers=header_carrier(mint_token(session_id=session_id)),
    ) as ws:
        for frame in chunks(audio):
            ws.send_bytes(frame)
        received = read_published(subscriber, expected=1)

    assert received == [
        {
            "session_id": str(session_id),
            "result_id": "r1",
            "text": "Let's get an MRI of that left knee.",
            "is_partial": False,
            "start_time": 0.5,
            "end_time": 3.25,
        }
    ]
    assert factory.stream.audio == audio
    assert len(factory.stream.audio) == 10 * 16_000 * 2


def test_partials_never_reach_the_bus(
    app_client: tuple[TestClient, RecordingFactory],
    subscriber: sync_redis.client.PubSub,
) -> None:
    """The rule holds against the real client, not only against the fake one."""
    client, factory = app_client
    session_id = uuid.uuid4()
    subscriber.subscribe(f"transcription:{session_id}")

    factory.stream.script(
        TranscriptSegment(result_id="r1", text="Let's get an", is_partial=True),
        TranscriptSegment(result_id="r1", text="Let's get an MRI.", is_partial=False),
    )

    with client.websocket_connect(
        f"/ws/audio/{session_id}",
        headers=header_carrier(mint_token(session_id=session_id)),
    ) as ws:
        ws.send_bytes(b"\x00\x01" * 100)
        received = read_published(subscriber, expected=1)

    assert [message["text"] for message in received] == ["Let's get an MRI."]


def test_a_refused_connection_publishes_nothing(
    app_client: tuple[TestClient, RecordingFactory],
    subscriber: sync_redis.client.PubSub,
) -> None:
    """No token, no transcription stream, no traffic on that session's channel."""
    client, factory = app_client
    session_id = uuid.uuid4()
    subscriber.subscribe(f"transcription:{session_id}")

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/audio/{session_id}"):
            pass  # pragma: no cover - the handshake never completes

    assert read_published(subscriber, expected=1, attempts=4) == []
    assert factory.calls == 0

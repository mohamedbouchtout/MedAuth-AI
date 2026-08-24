"""Fakes that let the audio WebSocket be exercised without AWS or Redis.

The transcription fake is deliberately narrow. It records what it was given and
lets the test decide what comes back; it does not model Transcribe's behaviour,
because nothing in this service depends on that behaviour beyond "audio goes in,
segments come out, the stream ends". A richer fake would mostly test itself.

The one piece of real timing it does model: by default ``segments()`` does not
finish until the input side is ended. That is what a streaming transcriber does,
and without it the publisher task would race the receive loop and the unit tests
would be flaky about how many segments arrived before the client disconnected.

``wait_for_end=False`` turns that off, for the integration suite. There the
segments have to be published *while the socket is still open*: the synchronous
``TestClient`` cancels the application task as soon as the WebSocket context
exits, so anything the service does after the disconnect — including opening its
first real Redis connection — can be cancelled before it completes. Emitting
mid-stream is also closer to what Transcribe actually does.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from src.api import websocket as websocket_module
from src.api.dependencies import get_redis, get_transcription_factory
from src.auth import JWT_SUBPROTOCOL_PREFIX, SESSION_SUBPROTOCOL
from src.config import JWT_ALGORITHM, get_settings
from src.main import create_app
from src.transcription import TranscriptSegment

#: 34 characters, comfortably over the 32-byte floor both services enforce.
SIGNING_KEY = "audio-route-test-signing-key-32byt"


class FakeTranscriptionStream:
    """Records audio pushed at it and replays a scripted list of segments."""

    def __init__(
        self,
        segments: list[TranscriptSegment] | None = None,
        *,
        wait_for_end: bool = True,
    ) -> None:
        self.sent: list[bytes] = []
        self.input_ended = False
        self.closed = False
        self._segments = list(segments or [])
        self._wait_for_end = wait_for_end
        # ``asyncio.Event`` rather than ``anyio.Event``: this is constructed by a
        # synchronous fixture, and an anyio event needs a running event loop to
        # exist at all. Since 3.10 an asyncio event binds its loop on first await,
        # so it can be built outside one.
        self._input_done = asyncio.Event()

    @property
    def audio(self) -> bytes:
        """Everything the route forwarded, in order."""
        return b"".join(self.sent)

    def script(self, *segments: TranscriptSegment) -> None:
        """Set what this stream will emit once the input side is ended."""
        self._segments = list(segments)

    async def send_audio(self, chunk: bytes) -> None:
        self.sent.append(chunk)

    async def end_input(self) -> None:
        self.input_ended = True
        self._input_done.set()

    async def close(self) -> None:
        self.closed = True
        self._input_done.set()

    async def segments(self) -> AsyncIterator[TranscriptSegment]:
        if self._wait_for_end:
            await self._input_done.wait()
        for segment in self._segments:
            yield segment


class RecordingFactory:
    """Stands in for the Transcribe Medical factory and counts its own calls.

    The call count is the assertion behind "no Transcribe stream is opened" for
    a refused connection — a claim about something *not* happening needs a
    witness.
    """

    def __init__(self, stream: FakeTranscriptionStream) -> None:
        self.stream = stream
        self.calls = 0

    async def __call__(self) -> FakeTranscriptionStream:
        self.calls += 1
        return self.stream


class FakeRedis:
    """Records published channels and payloads."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.published: list[tuple[str, str]] = []
        self.healthy = healthy

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("redis unreachable")
        return True


class RecordedAudit:
    """Captures audit calls instead of writing rows."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **fields: Any) -> None:
        self.calls.append(fields)


def mint_token(
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None = None,
    key: str = SIGNING_KEY,
    lifetime_seconds: int = 900,
) -> str:
    """Mint a session JWT the way track-a-clinical's TASK-006 endpoint does.

    Kept in this file rather than imported from ``track_a_clinical`` on purpose:
    this service validates a token that arrives over the wire, and a test that
    imported the issuer would stop noticing if the two drifted apart.
    """
    issued = datetime.datetime.now(datetime.UTC)
    claims = {
        "session_id": str(session_id),
        "provider_id": str(provider_id or uuid.uuid4()),
        "exp": int((issued + datetime.timedelta(seconds=lifetime_seconds)).timestamp()),
    }
    return jwt.encode(claims, key, algorithm=JWT_ALGORITHM)


def header_carrier(token: str) -> dict[str, str]:
    """The token as a service-to-service client sends it."""
    return {"Authorization": f"Bearer {token}"}


def subprotocol_carrier(token: str) -> list[str]:
    """The token as a browser sends it, having no way to set a header."""
    return [SESSION_SUBPROTOCOL, f"{JWT_SUBPROTOCOL_PREFIX}{token}"]


@pytest.fixture
def signing_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)
    yield SIGNING_KEY
    get_settings.cache_clear()


@pytest.fixture
def fake_stream() -> FakeTranscriptionStream:
    return FakeTranscriptionStream()


@pytest.fixture
def factory(fake_stream: FakeTranscriptionStream) -> RecordingFactory:
    return RecordingFactory(fake_stream)


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def recorded_audit(monkeypatch: pytest.MonkeyPatch) -> RecordedAudit:
    recorder = RecordedAudit()
    monkeypatch.setattr(websocket_module, "audit_audio_stream", recorder)
    return recorder


@pytest.fixture
def client(
    signing_key: str,
    factory: RecordingFactory,
    fake_redis: FakeRedis,
    recorded_audit: RecordedAudit,
) -> Iterator[TestClient]:
    """A test client bound to the app with AWS and Redis replaced."""
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    app.dependency_overrides[get_transcription_factory] = lambda: factory
    with TestClient(app) as test_client:
        yield test_client

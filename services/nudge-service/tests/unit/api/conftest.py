"""Fakes that let the nudge socket be exercised without a Redis server.

The pub/sub fake is deliberately narrow. It records what was subscribed to and
lets a test hand messages to the relay; it does not model Redis delivery
semantics, because nothing in this service depends on them beyond "a message
arrives, it goes out of the socket".

The one piece of real behaviour it does model: ``get_message`` returns ``None``
when the queue is empty rather than blocking forever, which is what redis-py does
on a timeout. Without that the relay task would never yield and the disconnect
watcher would never get to run.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from session_auth import JWT_ALGORITHM, JWT_SUBPROTOCOL_PREFIX, SESSION_SUBPROTOCOL
from src.api import websocket as websocket_module
from src.api.dependencies import get_redis
from src.config import get_settings
from src.main import create_app

#: 34 characters, comfortably over the 32-byte floor every service enforces.
SIGNING_KEY = "nudge-route-test-signing-key-32byt"


class FakePubSub:
    """A subscription that replays messages a test queued onto it."""

    def __init__(self, *, subscribe_fails: bool = False) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False
        self.subscribe_fails = subscribe_fails
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def subscribe(self, *channels: str) -> None:
        if self.subscribe_fails:
            raise ConnectionError("redis went away")
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.unsubscribed.extend(channels)

    async def aclose(self) -> None:
        self.closed = True

    def deliver(self, data: bytes | str, *, message_type: str = "message") -> None:
        """Queue one message for the relay to pick up, as redis-py would hand it."""
        self._queue.put_nowait({"type": message_type, "data": data})

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = False,
        # ASYNC109 wants asyncio.timeout instead of a timeout parameter. This is
        # a fake standing in for redis-py's own method, whose signature takes
        # one; changing it here would mean the fake no longer accepts the call
        # the code under test actually makes.
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> dict[str, Any] | None:
        """Return a queued message, or None once the queue drains.

        The sleep is what lets the disconnect watcher run: without yielding, this
        coroutine would spin and starve the other task in the group.
        """
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0)
            return None


class FakeRedis:
    """Hands out one subscription and answers health checks."""

    def __init__(self, *, healthy: bool = True, subscribe_fails: bool = False) -> None:
        self.healthy = healthy
        self.pubsub_instance = FakePubSub(subscribe_fails=subscribe_fails)

    def pubsub(self) -> FakePubSub:
        return self.pubsub_instance

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
    imported the issuer would stop noticing if the two drifted apart. What proves
    the issuer and the validator agree is packages/session-auth's own contract
    test, which is where the validation now lives.
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
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def recorded_audit(monkeypatch: pytest.MonkeyPatch) -> RecordedAudit:
    recorder = RecordedAudit()
    monkeypatch.setattr(websocket_module, "audit_nudge_stream", recorder)
    return recorder


@pytest.fixture
def client(
    signing_key: str,
    fake_redis: FakeRedis,
    recorded_audit: RecordedAudit,
) -> Iterator[TestClient]:
    """A test client bound to the app with Redis replaced."""
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    with TestClient(app) as test_client:
        yield test_client

"""The ``Origin`` check on the audio handshake (TASK-041c).

The nudge socket carries the same check, and the reasoning is shared rather than
re-derived — see the handler's comment and CLAUDE.md, "CORS and browser
reachability". It is defence in depth, not a fix: no ambient credential exists
for a hostile page to ride, since the session token is never a cookie.

The case worth keeping in view is the last one. Every caller of this endpoint
today sends no ``Origin`` header at all, so a check that refused them would look
correct in review and break the entire service.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api.dependencies import get_redis, get_transcription_factory
from src.api.websocket import WS_CLOSE_UNAUTHORIZED
from src.config import get_settings
from src.main import create_app
from tests.unit.api.conftest import (
    SIGNING_KEY,
    FakeRedis,
    RecordedAudit,
    RecordingFactory,
    header_carrier,
    mint_token,
)

ALLOWED_ORIGIN = "https://app.example.com"
OTHER_ORIGIN = "https://evil.example.com"


@pytest.fixture
def origin_client(
    monkeypatch: pytest.MonkeyPatch,
    factory: RecordingFactory,
    fake_redis: FakeRedis,
    recorded_audit: RecordedAudit,
) -> Iterator[TestClient]:
    """A client whose service is configured with exactly one allowed origin."""
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ALLOWED_ORIGIN)

    app = create_app()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    app.dependency_overrides[get_transcription_factory] = lambda: factory
    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()


def url(session_id: uuid.UUID) -> str:
    return f"/ws/audio/{session_id}"


def test_a_configured_origin_connects(origin_client: TestClient) -> None:
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with origin_client.websocket_connect(
        url(session_id),
        headers={**header_carrier(token), "Origin": ALLOWED_ORIGIN},
    ) as socket:
        socket.close()


def test_an_unconfigured_origin_is_refused(
    origin_client: TestClient,
    factory: RecordingFactory,
    recorded_audit: RecordedAudit,
) -> None:
    """Refused before the handshake: no transcription stream is opened for it,
    and no audit row is written for a stream that never happened."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with pytest.raises(WebSocketDisconnect) as refused:
        with origin_client.websocket_connect(
            url(session_id),
            headers={**header_carrier(token), "Origin": OTHER_ORIGIN},
        ):
            pass  # pragma: no cover - the connection never opens

    assert refused.value.code == WS_CLOSE_UNAUTHORIZED
    assert factory.calls == 0
    assert recorded_audit.calls == []


def test_a_request_with_no_origin_header_still_connects(origin_client: TestClient) -> None:
    """Every caller of this endpoint today sends no ``Origin``.

    A check that refused them would break the real callers while stopping
    nothing — a non-browser client can send any origin it likes.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with origin_client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        socket.close()


def test_an_origin_is_refused_when_none_are_configured(client: TestClient) -> None:
    """The default fixture configures no origins, which answers no browser."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(
            url(session_id),
            headers={**header_carrier(token), "Origin": ALLOWED_ORIGIN},
        ):
            pass  # pragma: no cover - the connection never opens

    assert refused.value.code == WS_CLOSE_UNAUTHORIZED

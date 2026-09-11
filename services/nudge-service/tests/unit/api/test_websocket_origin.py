"""The ``Origin`` check on this service's handshakes (TASK-041c, TASK-041d).

A browser applies no CORS to a WebSocket upgrade, so the middleware the HTTP
services install never reaches these endpoints and the origin is checked in the
handler. These tests pin the three behaviours that matter and are easy to lose:
a configured origin gets in, an unconfigured one is refused before the
handshake, and a request with no ``Origin`` at all is still served — that last
one is every service-to-service and test caller in the repository.

**Every case runs against both streams.** The check moved into the shared
``serve_stream`` when TASK-041d factored the lifecycle, so a single path would
cover the logic — but it would not cover a *route* that failed to go through
that lifecycle. Parameterising over the paths this service serves means a third
stream added later is one entry away from being covered here, instead of
silently inheriting an assertion nobody extended.

This is defence in depth rather than a fix for a vulnerability; the reasoning is
in the handler's comment and in CLAUDE.md, "CORS and browser reachability".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api.dependencies import get_redis
from src.api.websocket import WS_CLOSE_UNAUTHORIZED
from src.config import get_settings
from src.main import create_app
from tests.unit.api.conftest import (
    SIGNING_KEY,
    FakeRedis,
    RecordedAudit,
    header_carrier,
    mint_token,
)

ALLOWED_ORIGIN = "https://app.example.com"
OTHER_ORIGIN = "https://evil.example.com"

#: Each stream's URL prefix and the channel a connection to it subscribes to.
#: Parameterised together so a case cannot assert against the wrong channel.
STREAMS = [
    pytest.param("/ws/nudges", "nudges:{session_id}", id="nudges"),
    pytest.param("/ws/transcript", "transcription:{session_id}", id="transcript"),
]


@pytest.fixture
def origin_client(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    recorded_audit: RecordedAudit,
    recorded_transcript_audit: RecordedAudit,
) -> Iterator[TestClient]:
    """A client whose service is configured with exactly one allowed origin."""
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ALLOWED_ORIGIN)

    app = create_app()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()


@pytest.mark.parametrize(("prefix", "channel"), STREAMS)
def test_a_configured_origin_connects(
    origin_client: TestClient, fake_redis: FakeRedis, prefix: str, channel: str
) -> None:
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with origin_client.websocket_connect(
        f"{prefix}/{session_id}",
        headers={**header_carrier(token), "Origin": ALLOWED_ORIGIN},
    ):
        assert fake_redis.pubsub_instance.subscribed == [channel.format(session_id=session_id)]


@pytest.mark.parametrize(("prefix", "channel"), STREAMS)
def test_an_unconfigured_origin_is_refused(
    origin_client: TestClient,
    fake_redis: FakeRedis,
    recorded_audit: RecordedAudit,
    recorded_transcript_audit: RecordedAudit,
    prefix: str,
    channel: str,
) -> None:
    """Refused before the handshake, so nothing subscribes and nothing audits.

    The audit assertion is the one worth keeping: an accepted connection is a
    PHI access and writes a row, so a refusal that still audited would put a
    stream that never happened in the compliance trail. Both recorders are
    checked, because a refusal on one stream must not write the other's row
    either.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with pytest.raises(WebSocketDisconnect) as refused:
        with origin_client.websocket_connect(
            f"{prefix}/{session_id}",
            headers={**header_carrier(token), "Origin": OTHER_ORIGIN},
        ):
            pass  # pragma: no cover - the connection never opens

    assert refused.value.code == WS_CLOSE_UNAUTHORIZED
    assert fake_redis.pubsub_instance.subscribed == []
    assert recorded_audit.calls == []
    assert recorded_transcript_audit.calls == []


@pytest.mark.parametrize(("prefix", "channel"), STREAMS)
def test_a_valid_token_does_not_rescue_a_refused_origin(
    origin_client: TestClient, prefix: str, channel: str
) -> None:
    """The two checks are independent: the origin is not a credential, and a
    credential is not an origin."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with pytest.raises(WebSocketDisconnect):
        with origin_client.websocket_connect(
            f"{prefix}/{session_id}",
            headers={**header_carrier(token), "Origin": OTHER_ORIGIN},
        ):
            pass  # pragma: no cover - the connection never opens


@pytest.mark.parametrize(("prefix", "channel"), STREAMS)
def test_a_request_with_no_origin_header_still_connects(
    origin_client: TestClient,
    fake_redis: FakeRedis,
    prefix: str,
    channel: str,
) -> None:
    """Every service-to-service caller in this repository sends no ``Origin``.

    Refusing them would break the real callers while stopping nothing: a
    non-browser client can send whatever origin it likes. CORS constrains
    browsers on behalf of their users; it is not an access control here.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with origin_client.websocket_connect(f"{prefix}/{session_id}", headers=header_carrier(token)):
        assert fake_redis.pubsub_instance.subscribed == [channel.format(session_id=session_id)]


@pytest.mark.parametrize(("prefix", "channel"), STREAMS)
def test_an_origin_is_refused_when_none_are_configured(
    client: TestClient,
    fake_redis: FakeRedis,
    prefix: str,
    channel: str,
) -> None:
    """The default fixture configures no origins, which answers no browser.

    This is the case that actually guards against a permissive default shipping:
    an empty allow-list must refuse every browser rather than trusting one
    nobody chose.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(
            f"{prefix}/{session_id}",
            headers={**header_carrier(token), "Origin": ALLOWED_ORIGIN},
        ):
            pass  # pragma: no cover - the connection never opens

    assert refused.value.code == WS_CLOSE_UNAUTHORIZED

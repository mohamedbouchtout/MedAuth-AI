"""The nudge socket end to end: who gets in, what comes out, what is recorded.

The properties under test that are easy to lose in a refactor:

* the payload is relayed **byte for byte**, because this service must never
  become a second definition of TASK-040's shape;
* a token is refused **before** the handshake, so nothing subscribes for it;
* one audit row per accepted connection, none for a refused one.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from session_auth import SESSION_SUBPROTOCOL
from src.api.websocket import WS_CLOSE_UNAUTHORIZED
from src.audit import ACTION_RELAY_NUDGES
from tests.unit.api.conftest import (
    SIGNING_KEY,
    FakeRedis,
    RecordedAudit,
    header_carrier,
    mint_token,
    subprotocol_carrier,
)

#: A nudge exactly as TASK-040's emitter publishes it — CLAUDE.md, "The nudge
#: payload — one shape". Used as an opaque string; this service never reads it.
NUDGE = json.dumps(
    {
        "type": "PAYER_RULE_ALERT",
        "nudge_id": "0b7f0f3e-2f1a-4a0e-9a3a-2c9a9a6a1c11",
        "procedure": "knee MRI",
        "cpt_code": "73721",
        "message": "Prior authorization required for knee MRI.",
        "missing_criteria": ["six weeks of conservative therapy"],
        "denial_risk": "high",
        "haptic": True,
    }
)


def url(session_id: uuid.UUID) -> str:
    return f"/ws/nudges/{session_id}"


def test_a_published_nudge_reaches_the_client(client: TestClient, fake_redis: FakeRedis) -> None:
    """The acceptance criterion: publish to Redis, see it at the socket."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        fake_redis.pubsub_instance.deliver(NUDGE.encode())

        assert socket.receive_text() == NUDGE


def test_the_payload_is_relayed_byte_for_byte(client: TestClient, fake_redis: FakeRedis) -> None:
    """Not merely equivalent JSON — the same string.

    A relay that parsed and re-serialized would pass an equality check on the
    decoded object while silently becoming a second definition of the payload.
    This asserts on the string, so key order and spacing have to survive.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)
    odd_spacing = '{"denial_risk":"low",   "type":"PAYER_RULE_ALERT",\n"haptic":false}'

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        fake_redis.pubsub_instance.deliver(odd_spacing.encode())

        assert socket.receive_text() == odd_spacing


def test_a_payload_that_is_not_json_is_still_relayed(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """Verbatim means the relay has no opinion about the message at all.

    Dropping what it cannot parse would turn a formatting bug in the emitter into
    silence at the bedside, which a provider cannot tell from "nothing to flag".
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        fake_redis.pubsub_instance.deliver(b"not json at all")

        assert socket.receive_text() == "not json at all"


def test_several_nudges_arrive_in_order(client: TestClient, fake_redis: FakeRedis) -> None:
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        for index in range(3):
            fake_redis.pubsub_instance.deliver(f'{{"n":{index}}}'.encode())

        assert [socket.receive_text() for _ in range(3)] == ['{"n":0}', '{"n":1}', '{"n":2}']


def test_the_browser_carrier_is_accepted_and_the_version_marker_is_echoed(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """apps/web cannot set a header, and aborts if no offered subprotocol is echoed."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        url(session_id), subprotocols=subprotocol_carrier(token)
    ) as socket:
        fake_redis.pubsub_instance.deliver(NUDGE.encode())

        assert socket.receive_text() == NUDGE
        assert socket.accepted_subprotocol == SESSION_SUBPROTOCOL


def test_the_token_is_never_echoed_as_the_selected_subprotocol(client: TestClient) -> None:
    """Echoing it would copy a live credential into the response headers."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        url(session_id), subprotocols=subprotocol_carrier(token)
    ) as socket:
        assert socket.accepted_subprotocol is not None
        assert token not in socket.accepted_subprotocol


def test_the_subscription_is_this_session_and_not_a_pattern(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """A wildcard here would hand one client every encounter in the clinic."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)):
        pass

    assert fake_redis.pubsub_instance.subscribed == [f"nudges:{session_id}"]


def test_disconnect_unsubscribes_and_closes_the_subscription(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)):
        pass

    assert fake_redis.pubsub_instance.unsubscribed == [f"nudges:{session_id}"]
    assert fake_redis.pubsub_instance.closed is True


def test_an_inbound_frame_is_ignored_rather_than_closing_the_socket(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """This direction carries nothing; a client keepalive is not a broken client.

    Deliberately unlike the audio socket, where a text frame means the client is
    sending something other than audio and is closed with 1003.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        socket.send_text("ping")
        fake_redis.pubsub_instance.deliver(NUDGE.encode())

        assert socket.receive_text() == NUDGE


@pytest.mark.parametrize(
    "label",
    ["expired", "wrong key", "other session", "missing", "malformed"],
)
def test_a_bad_token_is_refused_with_4401(
    client: TestClient, fake_redis: FakeRedis, label: str
) -> None:
    """Every refusal path closes with the same code and subscribes to nothing."""
    session_id = uuid.uuid4()
    tokens: dict[str, str | None] = {
        "expired": mint_token(session_id=session_id, lifetime_seconds=-1),
        "wrong key": mint_token(session_id=session_id, key="a-different-key-padded-to-32-byte"),
        "other session": mint_token(session_id=uuid.uuid4()),
        "missing": None,
        "malformed": "not.a.jwt",
    }
    token = tokens[label]
    headers = header_carrier(token) if token is not None else {}

    with pytest.raises(WebSocketDisconnect) as refusal:
        with client.websocket_connect(url(session_id), headers=headers) as socket:
            socket.receive_text()

    assert refusal.value.code == WS_CLOSE_UNAUTHORIZED, label
    assert fake_redis.pubsub_instance.subscribed == [], label


def test_a_refused_connection_subscribes_to_nothing_and_audits_nothing(
    client: TestClient, fake_redis: FakeRedis, recorded_audit: RecordedAudit
) -> None:
    """No PHI was reached, so there is no access to record."""
    session_id = uuid.uuid4()

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url(session_id)) as socket:
            socket.receive_text()

    assert fake_redis.pubsub_instance.subscribed == []
    assert recorded_audit.calls == []


def test_an_accepted_connection_writes_exactly_one_audit_row(
    client: TestClient, fake_redis: FakeRedis, recorded_audit: RecordedAudit
) -> None:
    """One row per connection, not per relayed nudge."""
    session_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    token = mint_token(session_id=session_id, provider_id=provider_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        for index in range(3):
            fake_redis.pubsub_instance.deliver(f'{{"n":{index}}}'.encode())
        for _ in range(3):
            socket.receive_text()

    assert len(recorded_audit.calls) == 1
    assert recorded_audit.calls[0]["session_id"] == session_id
    assert recorded_audit.calls[0]["provider_id"] == provider_id


def test_the_audit_row_names_the_provider_from_the_token(
    client: TestClient, recorded_audit: RecordedAudit
) -> None:
    """This service reads no tables; the token's claim is the only actor it has."""
    session_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    token = mint_token(session_id=session_id, provider_id=provider_id, key=SIGNING_KEY)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)):
        pass

    assert recorded_audit.calls[0]["provider_id"] == provider_id


def test_the_action_constant_is_the_one_in_the_vocabulary() -> None:
    """CLAUDE.md's action vocabulary carries RELAY_NUDGES for this service."""
    assert ACTION_RELAY_NUDGES == "RELAY_NUDGES"


def test_an_undecodable_payload_is_skipped_and_the_next_one_still_arrives(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """A text frame carries UTF-8, so a non-UTF-8 publish cannot be relayed.

    Dropping that one message is right; dropping the connection with it would
    silence the rest of the encounter over a single malformed publish.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        fake_redis.pubsub_instance.deliver(b"\xff\xfe not utf-8")
        fake_redis.pubsub_instance.deliver(NUDGE.encode())

        assert socket.receive_text() == NUDGE


def test_a_subscribe_confirmation_is_not_relayed_to_the_client(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """Confirmations share the connection with real messages."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        fake_redis.pubsub_instance.deliver(b"1", message_type="subscribe")
        fake_redis.pubsub_instance.deliver(NUDGE.encode())

        assert socket.receive_text() == NUDGE


def test_a_failed_subscription_closes_with_1011_after_the_handshake(
    signing_key: str, recorded_audit: RecordedAudit
) -> None:
    """The bus can go away between accepting the socket and subscribing to it.

    The handshake has already completed by then, so unlike a bad token this is a
    close code the client genuinely observes.
    """
    from src.api.dependencies import get_redis
    from src.api.websocket import WS_CLOSE_INTERNAL_ERROR
    from src.main import create_app

    session_id = uuid.uuid4()
    broken = FakeRedis(subscribe_fails=True)
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: broken

    with TestClient(app) as failing_client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with failing_client.websocket_connect(
                url(session_id), headers=header_carrier(mint_token(session_id=session_id))
            ) as socket:
                socket.receive_text()

    assert closed.value.code == WS_CLOSE_INTERNAL_ERROR
    # The connection was accepted before the failure, so the access happened and
    # is recorded. This is the one case where a row exists for a stream that
    # relayed nothing.
    assert len(recorded_audit.calls) == 1

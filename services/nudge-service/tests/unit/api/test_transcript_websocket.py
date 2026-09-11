"""The transcript socket end to end: who gets in, what comes out, what is recorded.

TASK-041d. The lifecycle this exercises is the one the nudge socket already uses
— :func:`src.api.websocket.serve_stream` — so these tests deliberately do not
re-prove the parts that are shared and already covered there: the task group, the
teardown paths, the subprotocol echo, the 1011 after a failed subscription.

What they *do* prove is everything that is specific to this stream, and every one
of these could be wrong while the nudge socket stayed green:

* the route is served at its own path and subscribes to the **transcript**
  channel, not the nudge one;
* a segment is relayed byte for byte, so this service never becomes a second
  definition of CLAUDE.md's "The transcript segment payload — one shape";
* the audit row is ``RELAY_TRANSCRIPT``, one per connection, and is not the
  nudge stream's row;
* a refused token subscribes to nothing and audits nothing.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from session_auth import SESSION_SUBPROTOCOL
from src.api.websocket import WS_CLOSE_UNAUTHORIZED
from tests.unit.api.conftest import (
    SIGNING_KEY,
    FakeRedis,
    RecordedAudit,
    header_carrier,
    mint_token,
    subprotocol_carrier,
)

#: A segment exactly as TASK-020's publisher emits it — CLAUDE.md, "The
#: transcript segment payload — one shape". Used as an opaque string; this
#: service never reads it. ``is_partial`` is false because the publisher drops
#: partials before they reach the bus.
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


def url(session_id: uuid.UUID) -> str:
    return f"/ws/transcript/{session_id}"


def test_a_published_segment_reaches_the_client(client: TestClient, fake_redis: FakeRedis) -> None:
    """The acceptance criterion: publish to Redis, see it at the socket."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        fake_redis.pubsub_instance.deliver(SEGMENT.encode())

        assert socket.receive_text() == SEGMENT


def test_the_segment_is_relayed_byte_for_byte(client: TestClient, fake_redis: FakeRedis) -> None:
    """Parsing and re-serializing would make this a second definition of the shape.

    Asserted on the exact string rather than on decoded JSON: a relay that
    round-tripped the payload would satisfy an equality check on the decoded
    object while quietly reshaping what a client receives.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)
    odd_spacing = '{"text":"hello",   "is_partial":false}\n'

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        fake_redis.pubsub_instance.deliver(odd_spacing.encode())

        assert socket.receive_text() == odd_spacing


def test_a_segment_that_is_not_json_is_still_relayed(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """Dropping it would turn a publisher bug into silence at the bedside."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        fake_redis.pubsub_instance.deliver(b"not json at all")

        assert socket.receive_text() == "not json at all"


def test_several_segments_arrive_in_order(client: TestClient, fake_redis: FakeRedis) -> None:
    """A transcript read out of order is a different transcript."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        for index in range(3):
            fake_redis.pubsub_instance.deliver(f'{{"text":"segment {index}"}}'.encode())

        received = [socket.receive_text() for _ in range(3)]

    assert received == ['{"text":"segment 0"}', '{"text":"segment 1"}', '{"text":"segment 2"}']


def test_the_subscription_is_the_transcript_channel_for_this_session(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """The one thing most likely to be wrong in a second stream: the channel.

    A copy of the nudge route that kept its channel template would authenticate,
    accept, audit as a transcript access and then relay nudges — green on every
    other assertion in this file.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)):
        pass

    assert fake_redis.pubsub_instance.subscribed == [f"transcription:{session_id}"]


def test_the_subscription_is_not_a_pattern(client: TestClient, fake_redis: FakeRedis) -> None:
    """A wildcard here would hand one client every encounter's speech."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)):
        pass

    assert "*" not in fake_redis.pubsub_instance.subscribed[0]


def test_the_browser_carrier_is_accepted_and_the_version_marker_is_echoed(
    client: TestClient,
) -> None:
    """apps/web has no way to set a header, so this carrier is its only route in."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        url(session_id), subprotocols=subprotocol_carrier(token)
    ) as socket:
        assert socket.accepted_subprotocol == SESSION_SUBPROTOCOL


def test_the_token_is_never_echoed_as_the_selected_subprotocol(client: TestClient) -> None:
    """Selecting it would write the credential into every proxy log on the path."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(
        url(session_id), subprotocols=subprotocol_carrier(token)
    ) as socket:
        assert socket.accepted_subprotocol is not None
        assert token not in socket.accepted_subprotocol


@pytest.mark.parametrize(
    ("description", "token_factory"),
    [
        ("signed with the wrong key", lambda sid: mint_token(session_id=sid, key="x" * 32)),
        ("expired", lambda sid: mint_token(session_id=sid, lifetime_seconds=-60)),
        ("naming another session", lambda _sid: mint_token(session_id=uuid.uuid4())),
    ],
)
def test_a_bad_token_is_refused_with_4401(
    client: TestClient,
    description: str,
    token_factory: object,
) -> None:
    """Refused before the handshake, so nothing subscribes for it."""
    session_id = uuid.uuid4()
    token = token_factory(session_id)  # type: ignore[operator]

    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(url(session_id), headers=header_carrier(token)):
            pass  # pragma: no cover - the connect itself raises

    assert refused.value.code == WS_CLOSE_UNAUTHORIZED, description


def test_a_refused_connection_subscribes_to_nothing_and_audits_nothing(
    client: TestClient,
    fake_redis: FakeRedis,
    recorded_transcript_audit: RecordedAudit,
) -> None:
    """No PHI was reached, so there is nothing to record — see src/audit.py."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id, key="x" * 32)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url(session_id), headers=header_carrier(token)):
            pass  # pragma: no cover - the connect itself raises

    assert fake_redis.pubsub_instance.subscribed == []
    assert recorded_transcript_audit.calls == []


def test_an_accepted_connection_writes_exactly_one_audit_row(
    client: TestClient,
    fake_redis: FakeRedis,
    recorded_transcript_audit: RecordedAudit,
) -> None:
    """One row per connection, not per segment — an encounter is hundreds."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        for index in range(5):
            fake_redis.pubsub_instance.deliver(f'{{"text":"{index}"}}'.encode())
        for _ in range(5):
            socket.receive_text()

    assert len(recorded_transcript_audit.calls) == 1
    assert recorded_transcript_audit.calls[0]["session_id"] == session_id


def test_the_audit_row_names_the_provider_from_the_token(
    client: TestClient, recorded_transcript_audit: RecordedAudit
) -> None:
    """This service reads no tables; the claim is what track-a-clinical recorded."""
    session_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    token = mint_token(session_id=session_id, provider_id=provider_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)):
        pass

    assert recorded_transcript_audit.calls[0]["provider_id"] == provider_id


def test_opening_the_transcript_stream_does_not_audit_as_a_nudge_access(
    client: TestClient,
    recorded_audit: RecordedAudit,
    recorded_transcript_audit: RecordedAudit,
) -> None:
    """The two streams are two disclosures and must stay distinguishable.

    Collapsing them would make "was this encounter's speech ever streamed to a
    client" unanswerable from the audit trail, which is the whole reason
    ``RELAY_TRANSCRIPT`` exists rather than a second use of ``RELAY_NUDGES``.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)):
        pass

    assert len(recorded_transcript_audit.calls) == 1
    assert recorded_audit.calls == []


def test_an_inbound_frame_is_ignored_rather_than_closing_the_socket(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """Nothing travels client to server, and a keepalive is not a broken client.

    Deliberately unlike the audio socket, where the frame *is* the payload and an
    unexpected one closes with 1003.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)) as socket:
        socket.send_text("ping")
        fake_redis.pubsub_instance.deliver(SEGMENT.encode())

        assert socket.receive_text() == SEGMENT


def test_disconnect_unsubscribes_and_closes_the_subscription(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """A connection must not leave a subscription behind on the shared client."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url(session_id), headers=header_carrier(token)):
        pass

    assert fake_redis.pubsub_instance.unsubscribed == [f"transcription:{session_id}"]
    assert fake_redis.pubsub_instance.closed is True


def test_the_signing_key_is_the_one_the_issuer_uses(client: TestClient) -> None:
    """A sanity check on the fixture, so a wrong-key test cannot pass vacuously."""
    session_id = uuid.uuid4()

    with client.websocket_connect(
        url(session_id), headers=header_carrier(mint_token(session_id=session_id, key=SIGNING_KEY))
    ) as socket:
        assert socket.accepted_subprotocol is None

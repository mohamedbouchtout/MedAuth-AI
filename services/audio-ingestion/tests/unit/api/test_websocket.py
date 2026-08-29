"""The audio WebSocket's contract: who gets in, what flows, what is torn down.

Both of TASK-020's acceptance tests live here — ten seconds of audio producing
transcript events, and a bad token producing 4401 with no transcription stream
opened — alongside the cases that make those two meaningful: the browser's
carrier, a token minted for a different session, and the teardown that the
"audio never persists" constraint depends on.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from session_auth import SESSION_SUBPROTOCOL
from src.api.websocket import (
    WS_CLOSE_INTERNAL_ERROR,
    WS_CLOSE_UNAUTHORIZED,
    WS_CLOSE_UNSUPPORTED_DATA,
    _close_quietly,
)
from src.audio import MAX_BUFFERED_BYTES
from src.transcription import TranscriptSegment
from tests.unit.api.conftest import (
    FakeRedis,
    FakeTranscriptionStream,
    RecordedAudit,
    RecordingFactory,
    header_carrier,
    mint_token,
    subprotocol_carrier,
)
from tests.wav import CHUNK_BYTES, chunks, pcm_payload, wav_bytes


def final(text: str, *, result_id: str = "r1") -> TranscriptSegment:
    """A stabilized segment — the kind that is published."""
    return TranscriptSegment(result_id=result_id, text=text, is_partial=False)


def partial(text: str, *, result_id: str = "r1") -> TranscriptSegment:
    """A revision in flight — the kind that is not."""
    return TranscriptSegment(result_id=result_id, text=text, is_partial=True)


def url_for(session_id: uuid.UUID) -> str:
    return f"/ws/audio/{session_id}"


def test_ten_seconds_of_audio_is_transcribed_and_published(
    client: TestClient,
    factory: RecordingFactory,
    fake_stream: FakeTranscriptionStream,
    fake_redis: FakeRedis,
) -> None:
    """TASK-020's first acceptance test, end to end through the route."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)
    audio = pcm_payload(wav_bytes(seconds=10))
    fake_stream.script(final("Let's order an MRI of the left knee."))

    with client.websocket_connect(url_for(session_id), headers=header_carrier(token)) as ws:
        for frame in chunks(audio):
            ws.send_bytes(frame)

    assert factory.calls == 1
    # Every byte the client sent reached the transcriber, re-chunked but intact.
    assert fake_stream.audio == audio
    assert len(fake_stream.audio) == 10 * 16_000 * 2

    assert len(fake_redis.published) == 1
    channel, payload = fake_redis.published[0]
    assert channel == f"transcription:{session_id}"
    assert json.loads(payload) == {
        "session_id": str(session_id),
        "result_id": "r1",
        "text": "Let's order an MRI of the left knee.",
        "is_partial": False,
        "start_time": None,
        "end_time": None,
    }


def test_expired_token_is_refused_and_opens_no_transcribe_stream(
    client: TestClient,
    factory: RecordingFactory,
    recorded_audit: RecordedAudit,
) -> None:
    """TASK-020's second acceptance test.

    The factory's call count is the load-bearing assertion: 4401 alone would
    still be satisfied by an implementation that opened a stream and then
    changed its mind.
    """
    session_id = uuid.uuid4()
    expired = mint_token(session_id=session_id, lifetime_seconds=-60)

    with pytest.raises(WebSocketDisconnect) as refusal:
        with client.websocket_connect(url_for(session_id), headers=header_carrier(expired)):
            pass  # pragma: no cover - the handshake never completes

    assert refusal.value.code == WS_CLOSE_UNAUTHORIZED
    assert factory.calls == 0
    assert recorded_audit.calls == []


@pytest.mark.parametrize(
    ("label", "make_headers"),
    [
        ("garbage", lambda _sid: header_carrier("not-a-jwt")),
        ("no bearer prefix", lambda _sid: {"Authorization": mint_token(session_id=_sid)}),
        ("nothing at all", lambda _sid: {}),
        (
            "signed with another key",
            lambda _sid: header_carrier(mint_token(session_id=_sid, key="a" * 40)),
        ),
        (
            "minted for a different session",
            lambda _sid: header_carrier(mint_token(session_id=uuid.uuid4())),
        ),
    ],
)
def test_unusable_tokens_are_all_refused(
    client: TestClient,
    factory: RecordingFactory,
    label: str,
    make_headers: object,
) -> None:
    """Every way a token can be unusable ends the same way, and opens nothing."""
    session_id = uuid.uuid4()
    headers = make_headers(session_id)  # type: ignore[operator]

    with pytest.raises(WebSocketDisconnect) as refusal:
        with client.websocket_connect(url_for(session_id), headers=headers):
            pass  # pragma: no cover - the handshake never completes

    assert refusal.value.code == WS_CLOSE_UNAUTHORIZED, label
    assert factory.calls == 0, label


def test_browser_carrier_is_accepted_and_the_version_marker_is_echoed(
    client: TestClient,
    fake_stream: FakeTranscriptionStream,
    fake_redis: FakeRedis,
) -> None:
    """The subprotocol carrier works, and the token is never echoed back.

    A browser has no way to set a header, so this is the only carrier apps/web
    can use. Echoing the ``medauth.jwt.`` entry would copy a live credential into
    the handshake response headers.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)
    fake_stream.script(final("Ordering a knee arthroscopy."))

    with client.websocket_connect(
        url_for(session_id),
        subprotocols=subprotocol_carrier(token),
    ) as ws:
        ws.send_bytes(b"\x00\x01" * 100)
        accepted = ws.accepted_subprotocol

    assert accepted == SESSION_SUBPROTOCOL
    assert token not in str(accepted)
    assert len(fake_redis.published) == 1


def test_partial_segments_are_not_published(
    client: TestClient,
    fake_stream: FakeTranscriptionStream,
    fake_redis: FakeRedis,
) -> None:
    """Revisions in flight stay off the bus; only the stabilized result lands.

    Publishing partials would make TASK-021 fire the same procedure keyword
    repeatedly as one sentence is re-transcribed.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)
    fake_stream.script(
        partial("Let's order"),
        partial("Let's order an M"),
        final("Let's order an MRI."),
    )

    with client.websocket_connect(url_for(session_id), headers=header_carrier(token)) as ws:
        ws.send_bytes(b"\x00\x01" * 100)

    assert [json.loads(payload)["text"] for _, payload in fake_redis.published] == [
        "Let's order an MRI."
    ]


def test_one_audit_row_per_connection_naming_the_session_and_provider(
    client: TestClient,
    recorded_audit: RecordedAudit,
    fake_stream: FakeTranscriptionStream,
) -> None:
    """The PHI access is recorded once, not once per segment."""
    session_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    token = mint_token(session_id=session_id, provider_id=provider_id)
    fake_stream.script(final("one"), final("two", result_id="r2"))

    with client.websocket_connect(url_for(session_id), headers=header_carrier(token)) as ws:
        ws.send_bytes(b"\x00\x01" * 100)

    assert len(recorded_audit.calls) == 1
    assert recorded_audit.calls[0]["session_id"] == session_id
    assert recorded_audit.calls[0]["provider_id"] == provider_id


def test_disconnect_ends_the_stream_and_clears_the_buffer(
    client: TestClient,
    fake_stream: FakeTranscriptionStream,
) -> None:
    """Teardown is what the "audio never persists" constraint rests on."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url_for(session_id), headers=header_carrier(token)) as ws:
        ws.send_bytes(b"\x00\x01" * 100)

    assert fake_stream.input_ended
    assert fake_stream.closed


def test_a_tail_shorter_than_a_chunk_is_still_transcribed(
    client: TestClient,
    fake_stream: FakeTranscriptionStream,
) -> None:
    """The last words of an encounter are as much of the record as the rest."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)
    tail = b"\x01\x02\x03\x04"

    with client.websocket_connect(url_for(session_id), headers=header_carrier(token)) as ws:
        ws.send_bytes(tail)

    assert fake_stream.audio == tail


def test_a_text_frame_closes_the_socket_as_unsupported_data(client: TestClient) -> None:
    """This socket carries audio. Anything else is a client bug, reported as one."""
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url_for(session_id), headers=header_carrier(token)) as ws:
        ws.send_text("this is not audio")
        message = ws.receive()

    assert message["type"] == "websocket.close"
    assert message["code"] == WS_CLOSE_UNSUPPORTED_DATA


def test_a_failing_transcriber_closes_the_socket_rather_than_hanging(
    client: TestClient,
    factory: RecordingFactory,
) -> None:
    """A stream that dies mid-encounter must not leave the client waiting."""

    class ExplodingStream(FakeTranscriptionStream):
        async def send_audio(self, chunk: bytes) -> None:
            raise RuntimeError("transcribe went away")

    factory.stream = ExplodingStream()
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url_for(session_id), headers=header_carrier(token)) as ws:
        # A whole chunk, deliberately. A smaller frame would sit in the buffer
        # and never reach the transcriber, so the failure under test would not
        # happen until disconnect and this test would wait forever for it.
        ws.send_bytes(b"\x00\x01" * (CHUNK_BYTES // 2))
        message = ws.receive()

    assert message["type"] == "websocket.close"
    assert message["code"] == WS_CLOSE_INTERNAL_ERROR


def test_an_oversized_frame_closes_the_socket_rather_than_being_buffered(
    client: TestClient,
) -> None:
    """One enormous frame must not become one enormous allocation of PHI.

    The ceiling is per connection, and a client cannot outrun it by streaming
    fast — frames are read and forwarded in sequence. What it catches is a single
    frame far larger than anything a capture client legitimately produces.
    """
    session_id = uuid.uuid4()
    token = mint_token(session_id=session_id)

    with client.websocket_connect(url_for(session_id), headers=header_carrier(token)) as ws:
        ws.send_bytes(b"\x00" * (MAX_BUFFERED_BYTES + 1))
        message = ws.receive()

    assert message["type"] == "websocket.close"
    assert message["code"] == WS_CLOSE_INTERNAL_ERROR


async def test_closing_a_socket_the_client_already_abandoned_is_not_an_error() -> None:
    """Otherwise this exception would replace the failure being reported."""

    class GoneWebSocket:
        async def close(self, code: int) -> None:
            raise RuntimeError('Cannot call "send" once a close message has been sent.')

    await _close_quietly(GoneWebSocket(), WS_CLOSE_INTERNAL_ERROR)  # type: ignore[arg-type]

"""``WebSocket /ws/audio/{session_id}`` — encounter audio in, transcript out.

The shape of one connection:

1. The session JWT is validated, from either carrier, **before the handshake is
   accepted**. A refused token never reaches a state where it could send a
   frame, and no transcription stream is opened for it. The validation itself
   lives in ``packages/session-auth``, shared with the nudge relay (TASK-041).
2. The handshake is accepted, echoing ``medauth.session.v1`` if the client
   offered subprotocols, and the access is written to the audit log.
3. Client frames accumulate in an in-memory buffer and are pushed to Transcribe
   Medical in fixed-size chunks. Nothing is written to disk at any point.
4. Segments coming back are published to ``transcription:{session_id}``, where
   track-a-clinical (TASK-030) and track-b-rag (TASK-021) pick them up
   independently.
5. On disconnect the buffer's remainder is flushed, the transcription stream is
   ended, and the buffer is explicitly cleared.

Steps 3 and 4 run concurrently in a task group: audio arrives and results come
back on their own schedules, and serialising them would add the whole
transcription latency to every frame. A failure in either cancels the other, so
a connection never half-survives.

**PHI discipline.** Audio bytes and transcript text pass through this module and
are never logged. Log lines here carry a session identifier, a byte count, or a
close reason — never content, and never the token.
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

import anyio
from fastapi import APIRouter, Depends, WebSocket
from redis.asyncio import Redis

from cors_policy import ORIGIN_REFUSED_REASON, is_allowed_origin
from session_auth import (
    WS_CLOSE_UNAUTHORIZED,
    SessionAuthError,
    SessionIdentity,
    extract_token,
    select_subprotocol,
    validate_token,
)
from src.api.dependencies import get_app_settings, get_redis, get_transcription_factory
from src.audio import AudioBuffer, AudioBufferOverflow
from src.audit import audit_audio_stream
from src.config import Settings
from src.publisher import publish_segment
from src.transcription import TranscriptionStream, TranscriptionStreamFactory

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audio"])

#: RFC 6455 1003: the endpoint received data of a type it cannot accept. Sent
#: when a client sends text frames on a socket that carries audio.
WS_CLOSE_UNSUPPORTED_DATA: Final = 1003

#: RFC 6455 1011: the server hit a condition that stopped it fulfilling the
#: request. Sent when transcription fails or the client outruns the buffer.
WS_CLOSE_INTERNAL_ERROR: Final = 1011


class UnsupportedFrame(Exception):
    """The client sent something other than binary audio."""


def _authenticate(websocket: WebSocket, session_id: str, settings: Settings) -> SessionIdentity:
    """Validate the connection's token. Raises ``SessionAuthError`` if unusable."""
    token = extract_token(
        authorization=websocket.headers.get("authorization"),
        subprotocols=list(websocket.scope.get("subprotocols") or []),
    )
    return validate_token(token, session_id=session_id, signing_key=settings.jwt_signing_key)


async def _receive_audio(
    websocket: WebSocket,
    stream: TranscriptionStream,
    buffer: AudioBuffer,
) -> None:
    """Pump client frames into the transcription stream until the client goes away.

    Returns normally on disconnect, having flushed whatever partial chunk was
    still buffered — the tail of an encounter is as much of the record as the
    rest of it.
    """
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            break

        chunk = message.get("bytes")
        if chunk is None:
            raise UnsupportedFrame("expected binary audio frames")

        buffer.write(chunk)
        for whole_chunk in buffer.take_chunks():
            await stream.send_audio(whole_chunk)

    remainder = buffer.drain()
    if remainder:
        await stream.send_audio(remainder)


async def _publish_segments(
    stream: TranscriptionStream,
    redis: Redis,
    session_id: str,
) -> None:
    """Publish every stabilized segment until the transcriber closes the stream."""
    async for segment in stream.segments():
        await publish_segment(redis, segment, session_id=session_id)


@router.websocket("/ws/audio/{session_id}")
async def audio_stream(
    websocket: WebSocket,
    session_id: str,
    settings: Annotated[Settings, Depends(get_app_settings)],
    redis: Annotated[Redis, Depends(get_redis)],
    open_stream: Annotated[TranscriptionStreamFactory, Depends(get_transcription_factory)],
) -> None:
    """Accept encounter audio and publish its transcript to Redis.

    The client streams 16kHz mono PCM as binary frames. The session JWT must be
    supplied either as ``Authorization: Bearer <jwt>`` or as a
    ``medauth.jwt.<jwt>`` entry in the subprotocol list, and must carry the same
    ``session_id`` as the URL.
    """
    # TASK-041c. Browsers apply no CORS to a WebSocket upgrade, so the policy
    # installed on the HTTP services does not reach this handshake and the
    # origin is checked here instead.
    #
    # **This is defence in depth, not the fix for a vulnerability.** The absence
    # of this check was not a cross-site WebSocket hijacking hole: that attack
    # works by riding ambient credentials, and this repository has none — the
    # session JWT travels in an ``Authorization`` header or the ``medauth.jwt.``
    # subprotocol, never a cookie, so a page that does not already hold a token
    # cannot open a socket by pointing a browser at one. Do not read the check
    # as evidence that it once could, and do not treat removing it as reopening
    # a hole. What would change that: the credential moving to a cookie. See
    # CLAUDE.md, "CORS and browser reachability".
    #
    # A refused origin closes with 4401 rather than a code of its own. The
    # client-visible outcome is identical either way — a connection refused
    # before the handshake completes has no frame to carry a code in — so a
    # second code would add surface without telling a client anything. The
    # operational trace is where the two are distinguished, by the fixed label.
    if not is_allowed_origin(websocket.headers.get("origin"), settings.cors_allowed_origins):
        logger.warning("Refused audio connection: %s", ORIGIN_REFUSED_REASON)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    try:
        identity = _authenticate(websocket, session_id, settings)
    except SessionAuthError as exc:
        # The reason is a fixed label, never the token or a claim value.
        logger.warning("Refused audio connection: %s", exc.reason)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    await websocket.accept(
        subprotocol=select_subprotocol(list(websocket.scope.get("subprotocols") or []))
    )
    await _audit_accepted_connection(websocket, identity)

    buffer = AudioBuffer()
    stream = await open_stream()
    # ``except*`` rather than ``except``: an anyio task group wraps whatever its
    # tasks raise in an ExceptionGroup, a single exception included, so a plain
    # ``except UnsupportedFrame`` here would never match and every failure would
    # escape as an unhandled group.
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_publish_segments, stream, redis, session_id)
            await _receive_audio(websocket, stream, buffer)
            await stream.end_input()
    except* UnsupportedFrame:
        logger.warning("Session %s sent a non-binary frame", session_id)
        await _close_quietly(websocket, WS_CLOSE_UNSUPPORTED_DATA)
    except* AudioBufferOverflow:
        logger.error("Session %s outran the transcription stream", session_id)
        await _close_quietly(websocket, WS_CLOSE_INTERNAL_ERROR)
    except* Exception:
        logger.exception("Transcription failed for session %s", session_id)
        await _close_quietly(websocket, WS_CLOSE_INTERNAL_ERROR)
    finally:
        # The constraint "audio never persists" is met here, explicitly, on every
        # path out of the handler — including the failing ones.
        buffer.clear()
        await stream.close()


async def _audit_accepted_connection(websocket: WebSocket, identity: SessionIdentity) -> None:
    """Write the one audit row this connection produces."""
    await audit_audio_stream(
        session_id=identity.session_id,
        provider_id=identity.provider_id,
        ip_address=websocket.client.host if websocket.client else None,
        user_agent=websocket.headers.get("user-agent"),
    )


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    """Close the socket, tolerating a client that already went away.

    Closing an already-closed connection raises, and that exception would
    replace the failure being reported with a less informative one.
    """
    try:
        await websocket.close(code=code)
    except RuntimeError:
        logger.debug("Client had already disconnected before close(%d)", code)


__all__ = [
    "WS_CLOSE_INTERNAL_ERROR",
    "WS_CLOSE_UNAUTHORIZED",
    "WS_CLOSE_UNSUPPORTED_DATA",
    "router",
]

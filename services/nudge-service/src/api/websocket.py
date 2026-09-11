"""The WebSocket streams this service relays from the Redis bus to one client.

Today that is ``/ws/nudges/{session_id}``. The whole connection lifecycle below
is written once and parameterised by a :class:`RelayedStream`, because the parts
that differ between one relayed channel and the next are the channel name, the
audit action and a label for the log lines — and nothing else.

**That shape is deliberate, not speculative generality.** TASK-041 established
the rule for exactly this situation when it extracted ``packages/session-auth``
rather than let ``audio-ingestion``'s validator be copied into this service: two
hand-maintained copies of one mechanism diverge, not on purpose, but because a
fix lands in whichever file the person had open. A second socket written
standalone here would have been a near-verbatim copy of a working one, with the
origin check, the pre-handshake refusal, the task group, the close codes and the
teardown all duplicated.

The shape of one connection:

1. The ``Origin`` is checked, then the session JWT is validated from either
   carrier, both **before the handshake is accepted**. A refused peer never
   reaches a state where it could send a frame, and no subscription is opened for
   it. The validation is ``packages/session-auth``, shared with the audio socket
   (TASK-020) rather than reimplemented here — see CLAUDE.md, "How the JWT
   reaches a WebSocket endpoint".
2. The handshake is accepted, echoing ``medauth.session.v1`` if the client
   offered subprotocols, and the access is written to the audit log.
3. The service subscribes to the stream's channel for that session and forwards
   each message to the client verbatim.
4. On disconnect it unsubscribes and closes the subscription.

Steps 3 and 4 need two things watched at once — the bus and the socket — so they
run in a task group. The relay task would otherwise block forever on a quiet
encounter and never notice the client had gone.

**These sockets are one-directional.** Nothing in the protocol travels client to
server, so inbound frames are read only to notice the disconnect and their
contents are discarded. That is a deliberate difference from the audio socket,
which closes with 1003 on an unexpected frame type: there the frame *is* the
payload, so a text frame means a broken client, while here a client sending a
keepalive is not doing anything wrong and disconnecting it would be hostile.

**PHI discipline.** Relayed payload text passes through this module and is never
logged. Log lines here carry a stream label, a channel name, a session identifier
or a close reason — never content, and never the token.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Final, Protocol

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
from src import relay
from src.api.dependencies import get_app_settings, get_redis
from src.audit import audit_nudge_stream, audit_transcript_stream
from src.config import Settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streams"])

#: RFC 6455 1011: the server hit a condition that stopped it fulfilling the
#: request. Sent when the subscription fails after the handshake was accepted.
WS_CLOSE_INTERNAL_ERROR: Final = 1011


class StreamAudit(Protocol):
    """What this module needs of the audit function for one stream.

    A protocol rather than a concrete reference so :class:`RelayedStream` can
    hold the audit function for its own stream without this module deciding
    which, and so a double substituted for one in a test is checked against the
    same call shape the real function has.
    """

    async def __call__(
        self,
        *,
        session_id: uuid.UUID,
        provider_id: uuid.UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Record that one encounter's stream was opened to a client."""
        ...


@dataclass(frozen=True)
class RelayedStream:
    """One channel family this service relays, and the three things that vary.

    Everything else about serving a connection — the origin check, the token
    validation, the accept, the task group, the close codes, the teardown — is
    identical for every stream and lives in :func:`serve_stream`.
    """

    #: Names the stream in log lines, e.g. ``"nudge"``. Never user-supplied.
    label: str
    #: A canonical Redis key template from CLAUDE.md's key list.
    channel_template: str
    #: Writes the one audit row an accepted connection produces.
    audit: StreamAudit


#: The nudge stream (TASK-041). Looked up from module globals by the route below
#: rather than closed over, so a test can substitute one with a recording audit
#: double through ``monkeypatch.setattr``.
NUDGE_STREAM: Final = RelayedStream(
    label="nudge",
    channel_template=relay.NUDGE_CHANNEL_TEMPLATE,
    audit=audit_nudge_stream,
)

#: The transcript stream (TASK-041d). Same three fields, different values —
#: which is the whole of what the second stream needed once the lifecycle above
#: was written once.
TRANSCRIPT_STREAM: Final = RelayedStream(
    label="transcript",
    channel_template=relay.TRANSCRIPT_CHANNEL_TEMPLATE,
    audit=audit_transcript_stream,
)


def _authenticate(websocket: WebSocket, session_id: str, settings: Settings) -> SessionIdentity:
    """Validate the connection's token. Raises ``SessionAuthError`` if unusable."""
    token = extract_token(
        authorization=websocket.headers.get("authorization"),
        subprotocols=list(websocket.scope.get("subprotocols") or []),
    )
    return validate_token(token, session_id=session_id, signing_key=settings.jwt_signing_key)


async def _relay_messages(pubsub: Any, websocket: WebSocket, channel: str) -> None:
    """Forward every published message to the client until cancelled.

    The payload is sent exactly as it arrived. See :mod:`src.relay` for why this
    module never parses it. ``channel`` is passed down only so a dropped payload
    can be logged against the stream it came from.
    """
    while True:
        message = await pubsub.get_message(
            ignore_subscribe_messages=True,
            timeout=relay.READ_TIMEOUT_SECONDS,
        )
        if not relay.is_published_message(message):
            continue

        payload = relay.decode_payload(message.get("data"), channel=channel)
        if payload is None:
            continue

        await websocket.send_text(payload)


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    """Return when the client goes away.

    Inbound frames are drained and discarded: these sockets carry nothing in that
    direction, and a client that sends a keepalive should not be disconnected for
    it.
    """
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def serve_stream(
    websocket: WebSocket,
    session_id: str,
    settings: Settings,
    redis: Redis,
    stream: RelayedStream,
) -> None:
    """Authenticate a connection and relay one stream to it until it goes away.

    The whole lifecycle for every relayed stream. A route function's only job is
    to name which :class:`RelayedStream` it serves.
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
        logger.warning("Refused %s connection: %s", stream.label, ORIGIN_REFUSED_REASON)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    try:
        identity = _authenticate(websocket, session_id, settings)
    except SessionAuthError as exc:
        # The reason is a fixed label, never the token or a claim value.
        logger.warning("Refused %s connection: %s", stream.label, exc.reason)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    await websocket.accept(
        subprotocol=select_subprotocol(list(websocket.scope.get("subprotocols") or []))
    )
    await _audit_accepted_connection(websocket, identity, stream)

    channel = relay.channel_for(stream.channel_template, identity.session_id)
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(channel)
        logger.info("Relaying %s stream for session %s", stream.label, identity.session_id)
        # ``except*`` rather than ``except``: an anyio task group wraps whatever
        # its tasks raise in an ExceptionGroup, a single exception included, so a
        # plain ``except Exception`` here would never match.
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_relay_messages, pubsub, websocket, channel)
            await _wait_for_disconnect(websocket)
            # The relay task waits on a bus that may stay quiet for the rest of
            # the encounter; nothing else would ever end it.
            tasks.cancel_scope.cancel()
    except* Exception:
        logger.exception("The %s relay failed for session %s", stream.label, identity.session_id)
        await _close_quietly(websocket, WS_CLOSE_INTERNAL_ERROR)
    finally:
        # Unsubscribing before closing is what the task asks for explicitly. The
        # close alone would release the subscription, but only as a side effect
        # of tearing the connection down.
        await _release_quietly(pubsub, channel, stream.label)


@router.websocket("/ws/nudges/{session_id}")
async def nudge_stream(
    websocket: WebSocket,
    session_id: str,
    settings: Annotated[Settings, Depends(get_app_settings)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """Relay one encounter's clinical nudges to a connected client.

    The session JWT must be supplied either as ``Authorization: Bearer <jwt>`` or
    as a ``medauth.jwt.<jwt>`` entry in the subprotocol list, and must carry the
    same ``session_id`` as the URL. Each message is the nudge payload published by
    track-b-rag (TASK-040), forwarded unaltered.
    """
    await serve_stream(websocket, session_id, settings, redis, NUDGE_STREAM)


@router.websocket("/ws/transcript/{session_id}")
async def transcript_stream(
    websocket: WebSocket,
    session_id: str,
    settings: Annotated[Settings, Depends(get_app_settings)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """Relay one encounter's transcript segments to a connected client.

    TASK-041d. Authenticated exactly as the nudge socket is, and for the same
    reasons: the session JWT in either carrier, carrying the same ``session_id``
    as the URL. Each message is the segment payload published by audio-ingestion
    (TASK-020), forwarded unaltered — its shape is fixed in CLAUDE.md, "The
    transcript segment payload — one shape".

    **A client sees only what is said after it connects.** Redis pub/sub keeps no
    history, so a socket opened late or reopened after a drop starts from
    silence, and the earlier segments are not recoverable here — the accumulated
    transcript lives in track-a-clinical's in-memory buffer (TASK-030), which no
    route exposes. A client must therefore not present what it received as a
    complete transcript, and must show "connected, nothing said yet" differently
    from "not connected": an empty pane that reads as "nobody is speaking" is the
    one thing this stream must not be mistaken for.
    """
    await serve_stream(websocket, session_id, settings, redis, TRANSCRIPT_STREAM)


async def _audit_accepted_connection(
    websocket: WebSocket, identity: SessionIdentity, stream: RelayedStream
) -> None:
    """Write the one audit row this connection produces."""
    await stream.audit(
        session_id=identity.session_id,
        provider_id=identity.provider_id,
        ip_address=websocket.client.host if websocket.client else None,
        user_agent=websocket.headers.get("user-agent"),
    )


async def _release_quietly(pubsub: Any, channel: str, label: str) -> None:
    """Unsubscribe and close, tolerating a subscription already torn down.

    Runs on every path out of the handler, including the failing ones, so a
    connection cannot leave a subscription behind on the shared client.
    """
    try:
        await pubsub.unsubscribe(channel)
    except Exception:
        logger.debug("The %s subscription was already gone before unsubscribe", label)
    try:
        await pubsub.aclose()
    except Exception:
        logger.debug("The %s subscription was already closed", label)


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    """Close the socket, tolerating a client that already went away.

    Closing an already-closed connection raises, and that exception would replace
    the failure being reported with a less informative one.
    """
    try:
        await websocket.close(code=code)
    except RuntimeError:
        logger.debug("Client had already disconnected before close(%d)", code)

"""``WebSocket /ws/nudges/{session_id}`` — one encounter's nudges, live to a client.

The shape of one connection:

1. The session JWT is validated, from either carrier, **before the handshake is
   accepted**. A refused token never reaches a state where it could send a frame,
   and no subscription is opened for it. The validation is
   ``packages/session-auth``, shared with the audio socket (TASK-020) rather than
   reimplemented here — see CLAUDE.md, "How the JWT reaches a WebSocket
   endpoint".
2. The handshake is accepted, echoing ``medauth.session.v1`` if the client offered
   subprotocols, and the access is written to the audit log.
3. The service subscribes to ``nudges:{session_id}`` and forwards each message to
   the client verbatim.
4. On disconnect it unsubscribes and closes the subscription.

Steps 3 and 4 need two things watched at once — the bus and the socket — so they
run in a task group. The relay task would otherwise block forever on a quiet
encounter and never notice the client had gone.

**This socket is one-directional.** Nothing in the protocol travels client to
server, so inbound frames are read only to notice the disconnect and their
contents are discarded. That is a deliberate difference from the audio socket,
which closes with 1003 on an unexpected frame type: there the frame *is* the
payload, so a text frame means a broken client, while here a client sending a
keepalive is not doing anything wrong and disconnecting it would be hostile.

**PHI discipline.** Nudge text passes through this module and is never logged.
Log lines here carry a session identifier or a close reason — never content, and
never the token.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

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
from src.audit import audit_nudge_stream
from src.config import Settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["nudges"])

#: RFC 6455 1011: the server hit a condition that stopped it fulfilling the
#: request. Sent when the subscription fails after the handshake was accepted.
WS_CLOSE_INTERNAL_ERROR: Final = 1011


def _authenticate(websocket: WebSocket, session_id: str, settings: Settings) -> SessionIdentity:
    """Validate the connection's token. Raises ``SessionAuthError`` if unusable."""
    token = extract_token(
        authorization=websocket.headers.get("authorization"),
        subprotocols=list(websocket.scope.get("subprotocols") or []),
    )
    return validate_token(token, session_id=session_id, signing_key=settings.jwt_signing_key)


async def _relay_nudges(pubsub: Any, websocket: WebSocket) -> None:
    """Forward every published nudge to the client until cancelled.

    The payload is sent exactly as it arrived. See :mod:`src.relay` for why this
    module never parses it.
    """
    while True:
        message = await pubsub.get_message(
            ignore_subscribe_messages=True,
            timeout=relay.READ_TIMEOUT_SECONDS,
        )
        if not relay.is_nudge_message(message):
            continue

        payload = relay.decode_payload(message.get("data"))
        if payload is None:
            continue

        await websocket.send_text(payload)


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    """Return when the client goes away.

    Inbound frames are drained and discarded: this socket carries nothing in that
    direction, and a client that sends a keepalive should not be disconnected for
    it.
    """
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


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
        logger.warning("Refused nudge connection: %s", ORIGIN_REFUSED_REASON)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    try:
        identity = _authenticate(websocket, session_id, settings)
    except SessionAuthError as exc:
        # The reason is a fixed label, never the token or a claim value.
        logger.warning("Refused nudge connection: %s", exc.reason)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    await websocket.accept(
        subprotocol=select_subprotocol(list(websocket.scope.get("subprotocols") or []))
    )
    await _audit_accepted_connection(websocket, identity)

    channel = relay.channel_for(identity.session_id)
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(channel)
        logger.info("Relaying nudges for session %s", identity.session_id)
        # ``except*`` rather than ``except``: an anyio task group wraps whatever
        # its tasks raise in an ExceptionGroup, a single exception included, so a
        # plain ``except Exception`` here would never match.
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_relay_nudges, pubsub, websocket)
            await _wait_for_disconnect(websocket)
            # The relay task waits on a bus that may stay quiet for the rest of
            # the encounter; nothing else would ever end it.
            tasks.cancel_scope.cancel()
    except* Exception:
        logger.exception("Nudge relay failed for session %s", identity.session_id)
        await _close_quietly(websocket, WS_CLOSE_INTERNAL_ERROR)
    finally:
        # Unsubscribing before closing is what the task asks for explicitly. The
        # close alone would release the subscription, but only as a side effect
        # of tearing the connection down.
        await _release_quietly(pubsub, channel)


async def _audit_accepted_connection(websocket: WebSocket, identity: SessionIdentity) -> None:
    """Write the one audit row this connection produces."""
    await audit_nudge_stream(
        session_id=identity.session_id,
        provider_id=identity.provider_id,
        ip_address=websocket.client.host if websocket.client else None,
        user_agent=websocket.headers.get("user-agent"),
    )


async def _release_quietly(pubsub: Any, channel: str) -> None:
    """Unsubscribe and close, tolerating a subscription already torn down.

    Runs on every path out of the handler, including the failing ones, so a
    connection cannot leave a subscription behind on the shared client.
    """
    try:
        await pubsub.unsubscribe(channel)
    except Exception:
        logger.debug("Nudge subscription was already gone before unsubscribe")
    try:
        await pubsub.aclose()
    except Exception:
        logger.debug("Nudge subscription was already closed")


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    """Close the socket, tolerating a client that already went away.

    Closing an already-closed connection raises, and that exception would replace
    the failure being reported with a less informative one.
    """
    try:
        await websocket.close(code=code)
    except RuntimeError:
        logger.debug("Client had already disconnected before close(%d)", code)


__all__ = [
    "WS_CLOSE_INTERNAL_ERROR",
    "WS_CLOSE_UNAUTHORIZED",
    "router",
]

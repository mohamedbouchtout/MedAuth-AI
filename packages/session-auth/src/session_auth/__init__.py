"""Session-token validation shared by every real-time endpoint in the monorepo.

A WebSocket endpoint guards itself like this::

    from session_auth import (
        SessionAuthError,
        WS_CLOSE_UNAUTHORIZED,
        extract_token,
        select_subprotocol,
        validate_token,
    )

    try:
        token = extract_token(
            authorization=websocket.headers.get("authorization"),
            subprotocols=list(websocket.scope.get("subprotocols") or []),
        )
        identity = validate_token(token, session_id=session_id, signing_key=key)
    except SessionAuthError as exc:
        logger.warning("Refused connection: %s", exc.reason)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    await websocket.accept(subprotocol=select_subprotocol(offered))

**Scope note:** this package validates a session token and reads the two carriers
it can arrive in. It does not mint tokens, does not manage sessions, and is not a
place for shared routes, dependencies or middleware. ``POST /sessions/start`` in
``track-a-clinical`` (TASK-006) remains the only issuer, per Known Constraint 8
in TASKS.md.

It exists because ``audio-ingestion`` (TASK-020) and ``nudge-service``
(TASK-041) both need it and the second one would have started as a copy of the
first — the same condition that produced ``packages/api-envelope``, and the same
outcome. Two hand-maintained copies of one validator is how the "parallel auth
mechanism" Known Constraint 8 forbids arrives without anyone deciding to build
one: the copies do not diverge on purpose, they diverge because a fix lands in
the file the person was already looking at. Any real-time endpoint added later
imports from here rather than copying again.

The order of the two operations matters and is the caller's to get right:
**validation runs before the handshake is accepted, never after**, so a peer with
an unusable token never reaches a state where it can send a frame.
"""

from session_auth.carriers import (
    JWT_SUBPROTOCOL_PREFIX,
    SESSION_SUBPROTOCOL,
    WS_CLOSE_UNAUTHORIZED,
    extract_token,
    select_subprotocol,
)
from session_auth.tokens import (
    JWT_ALGORITHM,
    MIN_SIGNING_KEY_BYTES,
    SessionAuthError,
    SessionIdentity,
    validate_token,
)

__all__ = [
    "JWT_ALGORITHM",
    "JWT_SUBPROTOCOL_PREFIX",
    "MIN_SIGNING_KEY_BYTES",
    "SESSION_SUBPROTOCOL",
    "WS_CLOSE_UNAUTHORIZED",
    "SessionAuthError",
    "SessionIdentity",
    "extract_token",
    "select_subprotocol",
    "validate_token",
]

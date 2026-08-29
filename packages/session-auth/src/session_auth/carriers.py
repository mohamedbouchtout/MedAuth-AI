"""How the session token reaches a WebSocket endpoint, and how a refusal is signalled.

CLAUDE.md's "How the JWT reaches a WebSocket endpoint" fixes two carriers, either
one sufficient::

    Authorization: Bearer <jwt>
    Sec-WebSocket-Protocol: medauth.session.v1, medauth.jwt.<jwt>

The header is the obvious carrier and it is what service-to-service callers and
tests use. It is not available to a browser: the native ``WebSocket`` constructor
takes a URL and a subprotocol list and nothing else, and ``apps/web`` is required
to use the native API rather than a library that tunnels its own handshake.

This module is deliberately indifferent to which carrier was used, so neither can
drift into having weaker checks than the other — the failure the two-carrier
design is most exposed to.
"""

from __future__ import annotations

from typing import Final

from session_auth.tokens import SessionAuthError

#: Offered first by a browser client and echoed back by the server on accept. It
#: exists so the handshake has a subprotocol to select that is not the token.
SESSION_SUBPROTOCOL: Final = "medauth.session.v1"

#: Prefix of the subprotocol entry that carries the token itself. Everything
#: after it is the encoded JWT. Base64url and ``.`` are all legal in the RFC 6455
#: subprotocol token production, so a JWT needs no further encoding.
JWT_SUBPROTOCOL_PREFIX: Final = "medauth.jwt."

#: Close code for a refused session token. In the application range (4000-4999),
#: chosen to echo HTTP 401. Note what it can and cannot do: a connection refused
#: before the handshake completes has no frame to carry a code in, so a browser
#: sees a failed upgrade rather than an ``onclose`` with 4401. Accepting an
#: unauthenticated handshake purely so the rejection reads nicely would be the
#: worse trade — see CLAUDE.md, "How the JWT reaches a WebSocket endpoint".
WS_CLOSE_UNAUTHORIZED: Final = 4401

_BEARER_PREFIX: Final = "bearer "


def extract_token(*, authorization: str | None, subprotocols: list[str]) -> str:
    """Return the token from whichever carrier supplied it.

    Args:
        authorization: The ``Authorization`` request header, if any.
        subprotocols: The subprotocols the client offered, in its own order.

    Returns:
        The encoded JWT, unvalidated.

    Raises:
        SessionAuthError: Neither carrier supplied a token.
    """
    if authorization and authorization.lower().startswith(_BEARER_PREFIX):
        token = authorization[len(_BEARER_PREFIX) :].strip()
        if token:
            return token

    for offered in subprotocols:
        if offered.startswith(JWT_SUBPROTOCOL_PREFIX):
            token = offered[len(JWT_SUBPROTOCOL_PREFIX) :].strip()
            if token:
                return token

    raise SessionAuthError("missing_token")


def select_subprotocol(subprotocols: list[str]) -> str | None:
    """Return the subprotocol to echo on accept, or None if the client offered none.

    A browser aborts a connection whose handshake response does not name one of
    the subprotocols it offered, so an offer has to be answered. The answer is
    always the version marker and never the ``medauth.jwt.`` entry — echoing the
    latter would copy the credential into the response headers and from there
    into any proxy log on the path.
    """
    if SESSION_SUBPROTOCOL in subprotocols:
        return SESSION_SUBPROTOCOL
    # A client that offered only the token entry still needs an answer, and the
    # version marker is not among its offers, so nothing can be selected. Browsers
    # are expected to offer both; this keeps a header-carrier client that offered
    # nothing from being handed a subprotocol it never asked for.
    return None


__all__ = [
    "JWT_SUBPROTOCOL_PREFIX",
    "SESSION_SUBPROTOCOL",
    "WS_CLOSE_UNAUTHORIZED",
    "extract_token",
    "select_subprotocol",
]

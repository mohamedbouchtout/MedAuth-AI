"""Validation of the session JWT that guards the audio WebSocket.

This service mints nothing. ``track-a-clinical``'s ``POST /sessions/start``
(TASK-006) is the only issuer in the monorepo, per Known Constraint 8 in
TASKS.md, and what happens here is the mirror of it: verify the signature with
the shared ``JWT_SIGNING_KEY``, verify the token has not expired, and verify the
token was minted for the session whose URL is being opened.

**Two carriers, either sufficient.** The token arrives either in an
``Authorization: Bearer`` header or as an entry in the ``Sec-WebSocket-Protocol``
list, because a browser cannot set a header on the native ``WebSocket``
constructor and ``apps/web`` is required to use it. CLAUDE.md's "How the JWT
reaches a WebSocket endpoint" is the canonical statement of this; the code here
is deliberately indifferent to which one was used, so neither carrier can drift
into having weaker checks than the other.

**Nothing in this module logs a token, a claim, or a reason string containing
either.** A rejection reason names the *kind* of failure — expired, wrong
session, malformed — and the caller logs that. The token is a credential and the
``session_id`` in a claim identifies a patient encounter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

import jwt

from src.config import JWT_ALGORITHM, Settings

#: Offered first by a browser client and echoed back by the server on accept. It
#: exists so the handshake has a subprotocol to select that is not the token.
SESSION_SUBPROTOCOL: Final = "medauth.session.v1"

#: Prefix of the subprotocol entry that carries the token itself. Everything
#: after it is the encoded JWT. Base64url and ``.`` are all legal in the RFC 6455
#: subprotocol token production, so a JWT needs no further encoding.
JWT_SUBPROTOCOL_PREFIX: Final = "medauth.jwt."

_BEARER_PREFIX: Final = "bearer "


class SessionAuthError(Exception):
    """A connection attempt that must be refused.

    ``reason`` is a short machine-ish label safe to log. It never contains the
    token, and never contains a claim value.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SessionIdentity:
    """The parts of a validated token this service acts on."""

    session_id: uuid.UUID
    provider_id: uuid.UUID


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


def validate_token(token: str, *, session_id: str, settings: Settings) -> SessionIdentity:
    """Validate a session token against the session it is being used to open.

    Args:
        token: The encoded JWT from either carrier.
        session_id: The ``session_id`` path parameter of the WebSocket URL.
        settings: Supplies ``jwt_signing_key``.

    Returns:
        The identity the connection acts as.

    Raises:
        SessionAuthError: The token is malformed, expired, signed with the wrong
            key, missing a required claim, or minted for a different session.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_signing_key,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "session_id", "provider_id"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise SessionAuthError("expired") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise SessionAuthError("missing_claim") from exc
    except jwt.InvalidTokenError as exc:
        # Covers a bad signature, a malformed token and a wrong algorithm alike.
        # They are one reason on purpose: telling a caller which of the three it
        # was is a probing oracle and changes nothing it can legitimately do.
        raise SessionAuthError("invalid_token") from exc

    try:
        claimed_session = uuid.UUID(str(claims["session_id"]))
        provider = uuid.UUID(str(claims["provider_id"]))
    except (ValueError, TypeError) as exc:
        raise SessionAuthError("malformed_claim") from exc

    try:
        requested_session = uuid.UUID(session_id)
    except ValueError as exc:
        raise SessionAuthError("malformed_session_id") from exc

    # Compared as UUIDs rather than strings: the same session written with
    # different capitalisation or brace style is the same session, and rejecting
    # it would be an outage that looks like an attack.
    if claimed_session != requested_session:
        raise SessionAuthError("session_mismatch")

    return SessionIdentity(session_id=claimed_session, provider_id=provider)

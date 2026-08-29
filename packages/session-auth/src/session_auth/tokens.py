"""Validation of the session JWT that guards every real-time endpoint.

This module validates and never mints. ``track-a-clinical``'s
``POST /sessions/start`` (TASK-006) is the only issuer in the monorepo, per
Known Constraint 8 in TASKS.md, and what happens here is the mirror of it:
verify the signature with the shared ``JWT_SIGNING_KEY``, verify the token has
not expired, and verify the token was minted for the session whose URL is being
opened.

**Nothing in this module logs a token, a claim, or a reason string containing
either.** A rejection reason names the *kind* of failure — expired, wrong
session, malformed — and the caller logs that. The token is a credential and the
``session_id`` in a claim identifies a patient encounter.

**The issuer keeps its own copy of these two constants, deliberately.**
``track_a_clinical.config`` defines ``JWT_ALGORITHM`` and
``MIN_SIGNING_KEY_BYTES`` as well, and importing this package there was
considered and rejected: the issuer is not a consumer of this validator, and
making it depend on one would invert what Known Constraint 8 centralises. What
proves the two agree is not a shared literal but
``tests/unit/test_issuer_contract.py``, which feeds the real issuer's output to
this validator. A shared constant would prove less: two sides can agree on an
algorithm name and still disagree about the claim set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

import jwt

#: Matches ``track_a_clinical.config.MIN_SIGNING_KEY_BYTES``. HS256 with a secret
#: shorter than the digest weakens the MAC, and PyJWT warns about it. A validator
#: that accepted a key the issuer refuses would turn a configuration mistake into
#: a mystery at connection time, so every service's ``Settings`` uses this floor.
MIN_SIGNING_KEY_BYTES: Final = 32

#: The algorithm track-a-clinical signs with. Pinned rather than read from the
#: token: honouring the token's own ``alg`` header is how a validator gets talked
#: into accepting ``none`` or into verifying an RS256 token with the public key
#: as an HMAC secret.
JWT_ALGORITHM: Final = "HS256"


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
    """The parts of a validated token an endpoint acts on."""

    session_id: uuid.UUID
    provider_id: uuid.UUID


def validate_token(token: str, *, session_id: str, signing_key: str) -> SessionIdentity:
    """Validate a session token against the session it is being used to open.

    Args:
        token: The encoded JWT from either carrier.
        session_id: The ``session_id`` path parameter of the WebSocket URL.
        signing_key: The shared ``JWT_SIGNING_KEY``. Taken as a string rather
            than as a service's ``Settings`` object so that no service's
            configuration class becomes part of this package's interface.

    Returns:
        The identity the connection acts as.

    Raises:
        SessionAuthError: The token is malformed, expired, signed with the wrong
            key, missing a required claim, or minted for a different session.
    """
    try:
        claims = jwt.decode(
            token,
            signing_key,
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


__all__ = [
    "JWT_ALGORITHM",
    "MIN_SIGNING_KEY_BYTES",
    "SessionAuthError",
    "SessionIdentity",
    "validate_token",
]

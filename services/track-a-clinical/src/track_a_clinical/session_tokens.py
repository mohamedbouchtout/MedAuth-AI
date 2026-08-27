"""The session JWT: minting it, and validating one presented back for a re-mint.

This is the only place in the monorepo that issues one. ``audio-ingestion``
(TASK-020) and ``nudge-service`` (TASK-041) validate the result before accepting
a WebSocket; per Known Constraint 8 in TASKS.md nothing else mints its own.

The claim set is exactly ``{session_id, provider_id, exp}``. Anything added here
has to be added to both validators in the same change, so it stays this small
until a deliberate hardening task widens it.

:func:`validate_remint_credential` is the one place this service *reads* a token
rather than issuing one, for ``POST /sessions/{session_id}/token`` (TASK-006b).
It deliberately mirrors ``validate_token`` in ``audio-ingestion``'s
``src/auth.py`` — same signature check, same required claims, same session-match
check — and differs in exactly one respect: expiry is not fatal, because an
expired token is the whole reason a client is asking.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Final, TypedDict

import jwt

from track_a_clinical.config import JWT_ALGORITHM, Settings

#: Names of the claims this service issues, so a test can assert the set exactly
#: rather than only asserting the ones it happens to check.
CLAIM_NAMES: Final[frozenset[str]] = frozenset({"session_id", "provider_id", "exp"})


class SessionClaims(TypedDict):
    """The decoded body of a session JWT."""

    session_id: str
    provider_id: str
    exp: int


def mint_session_jwt(
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID,
    settings: Settings,
    now: datetime.datetime | None = None,
) -> str:
    """Return a signed session JWT for one encounter.

    Args:
        session_id: The encounter's ``session_id``, generated server-side.
        provider_id: The provider who started the session.
        settings: Supplies ``jwt_signing_key`` and ``session_ttl_seconds``.
        now: Issue time, for tests that need a fixed clock. Defaults to UTC now.

    Returns:
        The encoded token. Expiry is ``now + session_ttl_seconds``.
    """
    issued_at = now or datetime.datetime.now(datetime.UTC)
    expires_at = issued_at + datetime.timedelta(seconds=settings.session_ttl_seconds)
    claims: SessionClaims = {
        "session_id": str(session_id),
        "provider_id": str(provider_id),
        # PyJWT encodes a datetime `exp` to an integer POSIX timestamp itself, but
        # doing it here keeps the TypedDict honest about what lands in the token.
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(dict(claims), settings.jwt_signing_key, algorithm=JWT_ALGORITHM)


class RemintCredentialError(Exception):
    """A re-mint request whose presented token cannot authorise it.

    ``reason`` is a short machine-ish label safe to log. It never contains the
    token and never contains a claim value — same rule as ``SessionAuthError``
    in ``audio-ingestion``, and for the same reason: the token is a credential
    and ``session_id`` identifies a patient encounter.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_remint_credential(
    token: str,
    *,
    session_id: uuid.UUID,
    settings: Settings,
    now: datetime.datetime | None = None,
) -> SessionClaims:
    """Validate a session token presented to authorise re-minting its own session.

    **Why the expired token is the credential.** An endpoint that hands out
    session tokens must not be weaker than the sockets those tokens open, and it
    gains nothing by being stronger. ``audio-ingestion``'s validator proves
    possession of a token minted for one session and nothing more; no provider
    authentication exists anywhere in this repo yet, and ``POST /sessions/start``
    itself takes ``provider_id`` as an unauthenticated body field. Requiring a
    stronger credential here than for the thing being re-minted would be
    ceremony, and would block the endpoint on infrastructure that does not exist
    until SMART on FHIR lands in Phase 5.

    **What the grace window buys, precisely.** It is not a defence against a
    client chaining refreshes — a live client is *supposed* to chain, and doing
    so is no stronger than holding one socket open, which handshake-only
    validation already permits indefinitely. What it caps is how long a single
    *captured* token stays useful. That matters because nothing auto-completes an
    abandoned encounter: ``POST /sessions/{id}/end`` is the only writer of
    ``status='completed'``, so without this bound a token leaked from a visit
    nobody remembered to end would stay a valid re-mint credential forever.

    **What it does not do: re-minting revokes nothing.** There is no ``jti``, no
    ``iat`` and no server-side token store, so every token issued for this
    session inside the window is equally acceptable, including one a later
    re-mint has already superseded. That is inherent in accepting a bearer token
    as its own refresh credential and is recorded here rather than implied away.
    Tracked as issue #51, which carries the options for narrowing it; do not
    close that gap here without reading it first.

    Args:
        token: The encoded JWT the client presented, expired or not.
        session_id: The ``session_id`` path parameter being re-minted.
        settings: Supplies ``jwt_signing_key`` and ``session_remint_grace_seconds``.
        now: Evaluation time, for tests that need a fixed clock. Defaults to UTC now.

    Returns:
        The decoded claims, once accepted.

    Raises:
        RemintCredentialError: The token is malformed, signed with the wrong key,
            missing a required claim, minted for a different session, or expired
            longer ago than the grace window allows.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_signing_key,
            algorithms=[JWT_ALGORITHM],
            # Expiry is checked below against the grace window instead. Everything
            # else PyJWT verifies — signature above all — still applies.
            options={"require": sorted(CLAIM_NAMES), "verify_exp": False},
        )
    except jwt.MissingRequiredClaimError as exc:
        raise RemintCredentialError("missing_claim") from exc
    except jwt.InvalidTokenError as exc:
        # A bad signature, a malformed token and a wrong algorithm are one reason
        # on purpose: distinguishing them is a probing oracle and changes nothing
        # a legitimate caller can do. Same choice as audio-ingestion's validator.
        raise RemintCredentialError("invalid_token") from exc

    try:
        claimed_session = uuid.UUID(str(claims["session_id"]))
        provider_id = uuid.UUID(str(claims["provider_id"]))
        expires_at = int(claims["exp"])
    except (ValueError, TypeError) as exc:
        raise RemintCredentialError("malformed_claim") from exc

    # The check that stops a token for one encounter minting a token for another.
    # Compared as UUIDs rather than as text, matching audio-ingestion: the same
    # session written with different capitalisation is the same session.
    if claimed_session != session_id:
        raise RemintCredentialError("session_mismatch")

    evaluated_at = now or datetime.datetime.now(datetime.UTC)
    grace = datetime.timedelta(seconds=settings.session_remint_grace_seconds)
    if evaluated_at > datetime.datetime.fromtimestamp(expires_at, datetime.UTC) + grace:
        raise RemintCredentialError("expired_beyond_grace")

    return SessionClaims(
        session_id=str(claimed_session),
        provider_id=str(provider_id),
        exp=expires_at,
    )

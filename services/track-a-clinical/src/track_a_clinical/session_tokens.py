"""Minting of the session JWT that every real-time endpoint validates.

This is the only place in the monorepo that issues one. ``audio-ingestion``
(TASK-020) and ``nudge-service`` (TASK-041) validate the result before accepting
a WebSocket; per Known Constraint 8 in TASKS.md nothing else mints its own.

The claim set is exactly ``{session_id, provider_id, exp}``. Anything added here
has to be added to both validators in the same change, so it stays this small
until a deliberate hardening task widens it.
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

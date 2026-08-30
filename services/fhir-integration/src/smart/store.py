"""The two Redis records a SMART launch writes.

Both key patterns are fixed in CLAUDE.md's canonical Redis key list:

``fhir_launch:{state}``
    Transient. Holds ``iss``, ``ehr_type``, ``launch_id``, the discovered token
    endpoint and the PKCE ``code_verifier``, between the authorization redirect
    and the callback that consumes it. Deleted by that callback — see
    ``claim_launch()``.

``fhir_token:{launch_id}``
    The EHR access token, its FHIR base URL and its ``ehr_type``, at the TTL the
    EHR's own ``expires_in`` sets.

**``launch_id`` is not ``session_id``.** A SMART launch and an encounter session
are two different things with two different lifetimes, and at callback time no
encounter exists to key a token on — the launch precedes the visit. Settled in
CLAUDE.md under "A SMART launch is not an encounter session", which TASK-052b
and TASK-070 also cite. Nothing here should grow a ``session_id``.
"""

from __future__ import annotations

import uuid
from typing import Final

from pydantic import BaseModel
from redis.asyncio import Redis

from src.adapters.factory import EHRType

#: Key prefixes, from CLAUDE.md's canonical list.
LAUNCH_KEY_PREFIX: Final = "fhir_launch:"
TOKEN_KEY_PREFIX: Final = "fhir_token:"


def launch_key(state: str) -> str:
    """Return the Redis key holding one in-flight launch."""
    return f"{LAUNCH_KEY_PREFIX}{state}"


def token_key(launch_id: str) -> str:
    """Return the Redis key holding one launch's EHR access token."""
    return f"{TOKEN_KEY_PREFIX}{launch_id}"


class PendingLaunch(BaseModel):
    """A launch between the authorization redirect and the callback.

    ``code_verifier`` lives here and only here. It is the half of the PKCE pair
    that never travels on the redirect, so the record is what proves the
    callback belongs to the launch that started it.
    """

    launch_id: str
    iss: str
    ehr_type: EHRType
    code_verifier: str
    #: The token endpoint from the same discovery document that supplied the
    #: authorization endpoint this launch was sent to. Held rather than
    #: rediscovered at callback time for two reasons: a callback is a person
    #: waiting on a redirect and a second round trip to the EHR is latency
    #: nobody needs, and rediscovering would allow the two halves of one OAuth
    #: conversation to come from two different documents if a vendor rotated
    #: endpoints mid-flow.
    token_endpoint: str
    #: Whether the EHR supplied a ``launch`` parameter. Recorded because the
    #: scope string sent to the authorization endpoint depends on it, and the
    #: token exchange must not have to guess which launch type it is completing.
    ehr_launch: bool


class LaunchToken(BaseModel):
    """One launch's EHR credential and the context the token response carried."""

    ehr_type: EHRType
    fhir_base_url: str
    access_token: str
    #: Stored when the EHR returns one so TASK-051b has something to refresh
    #: without a second exchange. Nothing here renews a token.
    refresh_token: str | None = None
    #: The SMART launch context the token response carried, kept as opaque
    #: identifiers. This service never reads, returns or logs them: TASK-052 is
    #: the first reader, and the read is a PHI access that audits there. Storing
    #: them is what stops that task needing a second token exchange to learn
    #: which patient the EHR launched us for.
    patient_id: str | None = None
    encounter_id: str | None = None
    scope: str | None = None


def new_state() -> str:
    """Return a fresh OAuth ``state``.

    Server-generated, per the UUID convention in CLAUDE.md, and never
    client-supplied: ``state`` is this flow's CSRF defence, so a value a caller
    could choose would defend against nothing.
    """
    return str(uuid.uuid4())


def new_launch_id() -> str:
    """Return a fresh ``launch_id``, naming one SMART launch."""
    return str(uuid.uuid4())


async def save_pending_launch(
    redis: Redis,
    state: str,
    pending: PendingLaunch,
    *,
    ttl_seconds: int,
) -> None:
    """Record an in-flight launch against its ``state``.

    Args:
        redis: The Redis client.
        state: The OAuth ``state`` this launch will come back with.
        pending: What the callback needs to complete the exchange.
        ttl_seconds: How long the launch may sit unfinished.
    """
    await redis.set(launch_key(state), pending.model_dump_json(), ex=ttl_seconds)


async def claim_launch(redis: Redis, state: str) -> PendingLaunch | None:
    """Consume the launch record for one ``state``, atomically.

    A ``state`` is single-use. Reading and deleting in one round trip is what
    makes that true under concurrency: two callbacks arriving with the same
    ``state`` cannot both find a record, so a replayed callback cannot mint a
    second token. Leaving the record readable past its callback would keep a
    replayable CSRF token alive for the rest of its TTL.

    Args:
        redis: The Redis client.
        state: The ``state`` the callback presented.

    Returns:
        The pending launch, or ``None`` when no record exists — unknown,
        expired, or already consumed, which are one answer to the caller.
    """
    raw = await redis.getdel(launch_key(state))
    if raw is None:
        return None
    return PendingLaunch.model_validate_json(raw)


async def save_launch_token(
    redis: Redis,
    launch_id: str,
    token: LaunchToken,
    *,
    ttl_seconds: int,
) -> None:
    """Store one launch's EHR access token.

    Args:
        redis: The Redis client.
        launch_id: The launch this token belongs to.
        token: The credential and the context that came with it.
        ttl_seconds: The EHR's own ``expires_in``. The record expires with the
            token it holds rather than outliving it.
    """
    await redis.set(token_key(launch_id), token.model_dump_json(), ex=ttl_seconds)


async def load_launch_token(redis: Redis, launch_id: str) -> LaunchToken | None:
    """Return one launch's stored token, or ``None`` once it has expired."""
    raw = await redis.get(token_key(launch_id))
    if raw is None:
        return None
    return LaunchToken.model_validate_json(raw)

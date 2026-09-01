"""The two Redis records a SMART launch writes.

Both key patterns are fixed in CLAUDE.md's canonical Redis key list:

``fhir_launch:{state}``
    Transient. Holds ``iss``, ``ehr_type``, ``launch_id``, the discovered token
    endpoint and the PKCE ``code_verifier``, between the authorization redirect
    and the callback that consumes it. Deleted by that callback — see
    ``claim_launch()``.

``fhir_token:{launch_id}``
    The EHR access token, the refresh token that renews it, the token endpoint
    to renew it against, its FHIR base URL and its ``ehr_type``.

    **Its TTL bounds the refresh grant, not the access token** — TASK-051b,
    reversing what TASK-051 built. Keying the record to ``expires_in`` deleted
    the only copy of the refresh token at the exact moment renewal needed it, so
    the record has to outlive the credential it holds. The access token's own
    expiry is therefore a field, ``access_token_expires_at``, and never inferred
    from the key's remaining TTL. A record with no refresh token keeps the
    original behaviour and still expires with the access token, because there is
    nothing to renew and a patient identifier beside a dead credential earns
    nothing. See CLAUDE.md, "The launch record outlives its access token".

**``launch_id`` is not ``session_id``.** A SMART launch and an encounter session
are two different things with two different lifetimes, and at callback time no
encounter exists to key a token on — the launch precedes the visit. Settled in
CLAUDE.md under "A SMART launch is not an encounter session", which TASK-052b
and TASK-070 also cite. Nothing here should grow a ``session_id``.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis

from src.adapters.factory import EHRType

logger = logging.getLogger(__name__)

#: Key prefixes, from CLAUDE.md's canonical list.
LAUNCH_KEY_PREFIX: Final = "fhir_launch:"
TOKEN_KEY_PREFIX: Final = "fhir_token:"

#: How long a record lingers after its refresh grant has been refused. Long
#: enough that a client retrying immediately is told the launch expired rather
#: than that it never existed, and short enough that a dead credential and a
#: patient identifier are not held for the full grant window. A round default,
#: not a measurement.
REJECTED_GRANT_TTL_SECONDS: Final = 300


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
    #: When the access token above stops being good, absolute and in UTC.
    #: **A field rather than the key's remaining TTL**, because the record now
    #: outlives the token: one TTL cannot represent two lifetimes, and the
    #: reader that needs this value is the one deciding whether to renew.
    access_token_expires_at: datetime
    #: Where to renew against. Carried forward from ``PendingLaunch`` at callback
    #: rather than rediscovered, for that field's own reason: rediscovery would
    #: let two halves of one OAuth conversation come from two documents if a
    #: vendor rotated endpoints in between, and a refresh is as much a half of
    #: that conversation as the original exchange.
    token_endpoint: str
    #: The grant that renews the access token. ``None`` means an EHR that issued
    #: no refresh token, and the launch simply ends when the access token does.
    #: A server may hand back a *new* value on each renewal; whoever refreshes
    #: stores whichever it got, or the second refresh presents a token the
    #: server has already invalidated.
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


def access_token_expiry(ttl_seconds: int, *, now: datetime | None = None) -> datetime:
    """Return the absolute instant a token living ``ttl_seconds`` stops being good."""
    return (now or datetime.now(UTC)) + timedelta(seconds=ttl_seconds)


def record_ttl_seconds(
    token: LaunchToken,
    *,
    refresh_grant_ttl_seconds: int,
    now: datetime | None = None,
) -> int:
    """Return how long ``fhir_token:{launch_id}`` should live.

    The whole of TASK-051b's storage fix is this function. A record carrying a
    refresh token must outlive the access token, or the grant is deleted at the
    moment renewal needs it; a record carrying none must not, because there is
    nothing to renew and holding a patient identifier beside a dead credential
    buys nothing. Both readings of "how long" were previously collapsed into the
    EHR's ``expires_in``.

    Args:
        token: The record about to be written.
        refresh_grant_ttl_seconds: The configured bound on a refresh grant. SMART
            on FHIR gives no ``refresh_expires_in``, so this is a chosen bound
            rather than something the EHR told us.
        now: Injectable clock, for tests.

    Returns:
        A positive number of seconds, never zero — a record written with a
        non-positive TTL would be rejected by Redis rather than expiring.
    """
    remaining = math.ceil(
        (token.access_token_expires_at - (now or datetime.now(UTC))).total_seconds()
    )
    if token.refresh_token is None:
        return max(remaining, 1)
    return max(refresh_grant_ttl_seconds, remaining, 1)


def access_token_is_stale(
    token: LaunchToken,
    *,
    skew_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Whether the access token is expired, or close enough that it should renew.

    The margin exists because the EHR's clock is not ours: a token this service
    believes has seconds left may already be refused. It cannot close that gap
    entirely — skew larger than the margin still reaches the EHR as a 401, which
    is the named limit of proactive renewal in CLAUDE.md rather than an oversight
    here.
    """
    deadline = token.access_token_expires_at - timedelta(seconds=skew_seconds)
    return (now or datetime.now(UTC)) >= deadline


async def save_launch_token(
    redis: Redis,
    launch_id: str,
    token: LaunchToken,
    *,
    ttl_seconds: int,
) -> None:
    """Store one launch's EHR credential.

    Args:
        redis: The Redis client.
        launch_id: The launch this token belongs to. Unchanged by a renewal —
            it names the launch, not the token.
        token: The credential and the context that came with it.
        ttl_seconds: How long the record lives, from ``record_ttl_seconds()``.
            This is the refresh grant's bound, not the access token's; the
            access token's expiry is a field on the record.
    """
    await redis.set(token_key(launch_id), token.model_dump_json(), ex=ttl_seconds)


async def discard_refresh_grant(redis: Redis, launch_id: str, token: LaunchToken) -> None:
    """Record that this launch's refresh grant has been refused.

    Rewrites the record without its refresh token, at a short TTL. Deleting it
    outright was the alternative and is worse in both directions: the next
    request would get "no such launch" rather than "the launch expired, repeat
    it", and the launch would lose the ``ehr_type`` and endpoint an operator
    reads when working out why. Keeping it as it was is worse too — every later
    request would re-present a grant the vendor has just refused.
    """
    await save_launch_token(
        redis,
        launch_id,
        token.model_copy(update={"refresh_token": None}),
        ttl_seconds=REJECTED_GRANT_TTL_SECONDS,
    )


async def load_launch_token(redis: Redis, launch_id: str) -> LaunchToken | None:
    """Return one launch's stored token, or ``None`` when there is none to return.

    A record that does not parse is treated as absent rather than raised on. The
    caller's answer for "no such launch" is a 404, which is the truthful answer
    for a record this service cannot read — and the shape of this record changed
    in TASK-051b, so a record written by an older process is exactly the case
    that would otherwise surface as a 500. The launch id is never logged; it
    resolves to an EHR access token, which is what keeps it out of URLs too.
    """
    raw = await redis.get(token_key(launch_id))
    if raw is None:
        return None
    try:
        return LaunchToken.model_validate_json(raw)
    except ValidationError:
        logger.warning("Discarding an unreadable launch token record.")
        return None

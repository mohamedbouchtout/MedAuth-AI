"""The once-per-encounter guard on procedure keywords.

A clinician names a procedure more than once in a visit — proposing it,
confirming it, dictating it back. Every stabilized segment carrying the word
reaches this service, and without a guard each one costs a policy query and
raises another nudge for an order the provider has already been told about. The
first mention is the useful one; the rest are noise that trains people to
dismiss the banner without reading it.

**The claim is atomic, and that is the point.** ``SADD`` reports whether the
member was actually added, in one round trip, so two segments arriving close
together cannot both read "not seen yet" and both fire. A read-then-write pair
would have exactly that race, and the window is real: Transcribe emits
stabilized results in bursts.

**The state is in Redis, not in this process.** Every instance of this service
answers for every session, and CLAUDE.md's architecture keeps services
horizontally scalable, so an in-memory set would suppress a repeat only when the
repeat happened to land on the same pod. That is a guard that works on one
developer's laptop and stops working the day a second replica exists.

**One key per session rather than one per procedure**, so ending a session
clears the whole encounter's state with a single ``DEL``. The TTL underneath is
only a safety net for a session that never ends — a client that crashed
mid-visit — and not the mechanism.

**An unreachable Redis fails open.** A failed claim is treated as "first
mention", so the query fires and the provider may see one duplicate nudge.
Failing closed would suppress a nudge for a procedure nobody has been warned
about, which is the one direction this pipeline must never fail in. Cache-style
degradation, same as :mod:`track_b_rag.cache`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

#: From CLAUDE.md's canonical Redis key list. One set per session, holding the
#: procedure keys already queried during that encounter.
PROCEDURE_SEEN_TEMPLATE: Final = "procedure_seen:{session_id}"

#: Four hours. Not a tuning knob: the key is deleted when the session ends, so
#: this only bounds how long an abandoned encounter's guard survives. Long
#: enough that no real visit outlives it, short enough that a crashed client
#: does not leave state around for a day.
PROCEDURE_SEEN_TTL_SECONDS: Final = 4 * 60 * 60


def procedure_seen_key(session_id: uuid.UUID) -> str:
    """Return the guard key for one session."""
    return PROCEDURE_SEEN_TEMPLATE.format(session_id=session_id)


async def claim_procedure(client: Redis, session_id: uuid.UUID, procedure_key: str) -> bool:
    """Claim the first mention of `procedure_key` in this session.

    Args:
        client: Redis client.
        session_id: The encounter the mention belongs to.
        procedure_key: What counts as "the same procedure" for suppression. It
            is the canonical keyword — ``"MRI"`` — until TASK-024 supplies the
            keyword-to-CPT mapping, after which it becomes the CPT code. The
            parameter is deliberately an opaque string so that change is a
            change at the call site and not in here. The distinction matters:
            two keywords that map to one code should share a claim, and today
            they cannot, because nothing yet knows they are the same procedure.

    Returns:
        True when this is the first time the procedure has been claimed for the
        session and the caller should query, False when it has already been
        seen. An unreachable Redis returns True — see the module docstring for
        why the guard fails open.
    """
    key = procedure_seen_key(session_id)
    try:
        pipeline = client.pipeline()
        pipeline.sadd(key, procedure_key)
        pipeline.expire(key, PROCEDURE_SEEN_TTL_SECONDS)
        added, _ = await pipeline.execute()
    except RedisError:
        logger.warning(
            "Redis claim failed for session %s procedure %r; treating it as a first mention",
            session_id,
            procedure_key,
            exc_info=True,
        )
        return True

    return bool(added)


async def release_procedure(client: Redis, session_id: uuid.UUID, procedure_key: str) -> None:
    """Give back a claim whose policy query never happened.

    Called when the query failed for a reason that could succeed next time — a
    timeout, a transport error. Without this, one unlucky moment would suppress
    every later mention of that procedure for the rest of the encounter, and the
    provider would never be told about an order the service never actually
    checked.

    A claim released after a *structural* failure would be wrong instead: if the
    query cannot be built at all, releasing it just re-runs the same failure on
    every later mention. The caller makes that distinction, not this function.
    """
    try:
        await client.srem(procedure_seen_key(session_id), procedure_key)
    except RedisError:
        logger.warning(
            "Redis release failed for session %s procedure %r; the claim stands",
            session_id,
            procedure_key,
            exc_info=True,
        )


async def forget_session(client: Redis, session_id: uuid.UUID) -> None:
    """Drop a finished session's guard.

    Called on ``session:ended``. The TTL would eventually do this, but leaving
    hours of dead keys per encounter for a broker that also carries the live bus
    is untidy at the scale this is heading for.
    """
    try:
        await client.delete(procedure_seen_key(session_id))
    except RedisError:
        logger.warning(
            "Redis cleanup failed for session %s; its guard will expire on its TTL",
            session_id,
            exc_info=True,
        )

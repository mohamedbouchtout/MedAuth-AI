"""The audit writes for this service's PHI-touching work.

``POST /policies/query`` receives a ``clinical_context`` describing what a live
encounter has documented so far, so it touches PHI and Known Constraints #6
makes the ``audit_log()`` call mandatory. ``POST /policies/ingest`` does not and
must not — insurance policy documents are public payer publications, and mixing
operational writes into ``audit_log`` turns "who accessed patient X" from a
query you can run into one you have to filter. The two routes live in separate
modules partly so that difference stays visible.

The row records identifiers only: who asked, which session, which service. The
clinical context itself, the procedure, and the CPT code are all deliberately
absent — ``audit_log`` is a record that an access happened, and hipaa-logger's
own scope note is explicit that it never carries patient data.

``audit_policy_query`` does not join a caller's transaction, because the route
it serves writes no row of its own — there is no transaction to join, and the
audit goes through hipaa-logger's own pool.

``audit_nudge_write`` is the opposite case and takes a connection. It records a
row being written, so the two have to succeed or fail together: a nudge with no
audit row, and an audit row for a nudge that rolled back, are both worse than
the write failing outright.

The second one also has no request behind it. It is reached from a Redis
consumer, so CLAUDE.md's "Auditing work that no request triggered" applies: the
actor is the encounter's own ``provider_id``, and ``ip_address`` and
``user_agent`` are permanently absent rather than waiting on the middleware that
will fill them in for routes. There is no client to describe.

``audit_nudge_acknowledge`` (TASK-041b) is the third, and it is a route again —
a browser dismissing a nudge — so it takes the client fields back. What it does
*not* take is an actor from the caller: that route carries no credential in v1,
and the provider is read from the encounter the nudge belongs to. It writes one
of two actions depending on whether the row moved, which is the same call
track-a-clinical's idempotent session-end makes.
"""

from __future__ import annotations

import uuid
from typing import Final

import asyncpg

from hipaa_logger import audit_log

#: The audit vocabulary from CLAUDE.md's action table, not free text invented
#: per call site. Both of these are listed there — ``QUERY_POLICY`` only as of
#: TASK-040, which is the change that noticed TASK-012 had shipped it as a
#: constant citing a list that had never carried it.
ACTION_QUERY_POLICY: Final = "QUERY_POLICY"
ACTION_WRITE_NUDGE: Final = "WRITE_NUDGE"

#: The acknowledge route's pair (TASK-041b). Which one is written depends on
#: whether the row actually changed, exactly as an idempotent
#: ``POST /sessions/{id}/end`` audits as ``READ_ENCOUNTER`` rather than
#: ``END_SESSION``: a repeat dismissal read a row it did not move, and recording
#: it as a state change would make the trail claim something that did not
#: happen. ``READ_NUDGE`` is prior-auth's constant too — the table's "Written
#: by" column names both services.
ACTION_ACKNOWLEDGE_NUDGE: Final = "ACKNOWLEDGE_NUDGE"
ACTION_READ_NUDGE: Final = "READ_NUDGE"

SERVICE_NAME: Final = "track-b-rag"

#: The encounter is the resource being read: a policy query is answered from
#: what that encounter has documented, and ``session_id`` is the identifier that
#: encounter is known by everywhere else in the system — every Redis channel in
#: CLAUDE.md's canonical list keys off it.
RESOURCE_TYPE_ENCOUNTER: Final = "Encounter"

#: The nudge row itself is the resource written, so ``resource_id`` is its
#: primary key rather than the session — CLAUDE.md's rule for the field.
RESOURCE_TYPE_NUDGE: Final = "ClinicalNudge"


async def audit_policy_query(
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Record that a policy query read one encounter's clinical context.

    Raises whatever ``audit_log`` raises. An audit failure is never suppressed:
    HIPAA requires a durable record of every PHI access, so a failed write has
    to fail the request that could not be recorded.

    Args:
        session_id: The encounter session the clinical context belongs to.
        provider_id: The provider on whose behalf the query was made.
        ip_address: Client IP, when the transport has a peer.
        user_agent: Client user agent, when the caller sent one.
    """
    await audit_log(
        actor_id=str(provider_id),
        action=ACTION_QUERY_POLICY,
        resource_type=RESOURCE_TYPE_ENCOUNTER,
        resource_id=str(session_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        ip_address=ip_address,
        user_agent=user_agent,
    )


async def audit_nudge_write(
    *,
    nudge_id: uuid.UUID,
    session_id: uuid.UUID,
    provider_id: uuid.UUID,
    conn: asyncpg.Connection,
) -> None:
    """Record that a nudge was raised and stored against an encounter.

    Joins the transaction that writes the nudge, through ``conn``. Raises
    whatever ``audit_log`` raises, which rolls the nudge back with it.

    Records identifiers only. The procedure, the CPT code, the criteria and the
    message a provider was shown are all absent, exactly as they are from the
    policy query's row above — an audit row says an access happened, and the
    nudge row itself is where the clinical detail lives.

    Args:
        nudge_id: The stored ``clinical_nudges`` row, which is the resource.
        session_id: The encounter the nudge belongs to.
        provider_id: Read from the encounter row, never from a message payload.
            The Redis signal that started this work carries no identity, and a
            minted service-account UUID in an audit trail is worse than the
            honest null this field would otherwise hold.
        conn: The asyncpg connection the nudge is being written on.
    """
    await audit_log(
        actor_id=str(provider_id),
        action=ACTION_WRITE_NUDGE,
        resource_type=RESOURCE_TYPE_NUDGE,
        resource_id=str(nudge_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        # No client exists. See the module docstring — this is not the gap the
        # request-context middleware will close.
        ip_address=None,
        user_agent=None,
        conn=conn,
    )


async def audit_nudge_acknowledge(
    *,
    nudge_id: uuid.UUID,
    session_id: uuid.UUID,
    provider_id: uuid.UUID,
    changed: bool,
    conn: asyncpg.Connection,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Record that a provider dismissed a nudge, or re-read one already dismissed.

    Joins the transaction the acknowledgement is written on, through ``conn``,
    for the same reason ``audit_nudge_write`` does: an acknowledged nudge with
    no audit row, and an audit row for an acknowledgement that rolled back, are
    both worse than the write failing outright. Raises whatever ``audit_log``
    raises, which rolls the acknowledgement back with it.

    Unlike ``audit_nudge_write`` this one has a request behind it, so
    ``ip_address`` and ``user_agent`` are real parameters rather than
    permanently absent — a browser is the caller.

    Args:
        nudge_id: The ``clinical_nudges`` row, which is the resource.
        session_id: The encounter the nudge belongs to.
        provider_id: Read from the encounter row through the nudge, never from
            the caller. This route carries no credential in v1 (CLAUDE.md, "A
            route keyed on a resource rather than a session follows the same v1
            rule"), so the encounter is the only defensible source of an actor.
        changed: Whether this call moved the row. False on a repeat dismissal,
            which audits as a read.
        conn: The asyncpg connection the acknowledgement is being written on.
        ip_address: Client IP, when the transport has a peer.
        user_agent: Client user agent, when the caller sent one.
    """
    await audit_log(
        actor_id=str(provider_id),
        action=ACTION_ACKNOWLEDGE_NUDGE if changed else ACTION_READ_NUDGE,
        resource_type=RESOURCE_TYPE_NUDGE,
        resource_id=str(nudge_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        ip_address=ip_address,
        user_agent=user_agent,
        conn=conn,
    )

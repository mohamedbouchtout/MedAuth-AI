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

"""The audit write for this service's one PHI-touching route.

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

Unlike track-a-clinical's equivalent, this write does not join a caller's
transaction. That service audits alongside a row it is writing in the same
statement; this route writes nothing, so there is no transaction to join and
the audit goes through hipaa-logger's own pool.
"""

from __future__ import annotations

import uuid
from typing import Final

from hipaa_logger import audit_log

#: The audit vocabulary from CLAUDE.md's ``audit_log`` schema comment, not free
#: text invented per call site.
ACTION_QUERY_POLICY: Final = "QUERY_POLICY"

SERVICE_NAME: Final = "track-b-rag"

#: The encounter is the resource being read: a policy query is answered from
#: what that encounter has documented, and ``session_id`` is the identifier that
#: encounter is known by everywhere else in the system — every Redis channel in
#: CLAUDE.md's canonical list keys off it.
RESOURCE_TYPE_ENCOUNTER: Final = "Encounter"


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

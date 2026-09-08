"""Audit writes performed inside this service's own database transaction.

``hipaa_logger.audit_log`` writes on its own pool by default. The bundle write
instead passes the assembling session's connection, so the audit row and the
row it records commit or roll back together. A failed audit write raises out of
``audit_log`` and aborts the assembly — the intended behavior, not an edge case
to swallow.

Only identifiers are recorded here. No patient identifier, procedure, diagnosis
or note excerpt is ever passed as an audit field value beyond the resource id
the schema already calls for.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from hipaa_logger import AuditAction, audit_log
from prior_auth.db import raw_asyncpg_connection

#: The action this service records is an ``AuditAction`` member imported from
#: hipaa-logger rather than re-declared here: WRITE_PRIOR_AUTH. A local constant
#: per service is what let the vocabulary drift from its own definition three
#: times — see ``hipaa_logger.actions``.
SERVICE_NAME = "prior-auth"
RESOURCE_TYPE_PRIOR_AUTH_REQUEST = "PriorAuthRequest"


async def audit_bundle_write(
    session: AsyncSession,
    *,
    request_id: uuid.UUID,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None,
) -> None:
    """Record that a prior-authorization bundle was assembled and stored.

    This is CLAUDE.md's "Auditing work that no request triggered" applied
    unchanged: the trigger is a Redis signal rather than an HTTP request, and
    the "if and only if it touches PHI" test is what decides that a row is
    written. Assembly reads a whole encounter's note, its nudges and the
    patient's chart context, and writes a new PHI record — so it audits, and
    the absence of a caller is the reason no other record of that access exists.

    No ``ip_address`` or ``user_agent``: there is no client, and there never
    will be one for this path. They are permanently absent here rather than
    waiting on the request-context middleware that will populate them for
    routes.

    One row per bundle, never one per nudge or per code read on the way to it.
    The auditable access is the assembly.

    Args:
        session: The active session whose transaction the audit row joins.
        request_id: Primary key of the ``prior_auth_requests`` row written.
        session_id: The encounter's session identifier, for trace correlation.
        provider_id: Taken from the ``encounters`` row — the provider who opened
            the visit. Never a service account invented to fill the field: the
            column is nullable, and a fabricated identifier in an audit trail is
            worse than an honest null.
    """
    await audit_log(
        actor_id=str(provider_id) if provider_id else None,
        action=AuditAction.WRITE_PRIOR_AUTH,
        resource_type=RESOURCE_TYPE_PRIOR_AUTH_REQUEST,
        resource_id=str(request_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        conn=await raw_asyncpg_connection(session),
    )

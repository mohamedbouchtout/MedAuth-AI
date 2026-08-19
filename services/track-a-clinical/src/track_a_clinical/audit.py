"""Audit writes performed inside this service's own database transaction.

``hipaa_logger.audit_log`` writes on its own pool by default. Every PHI access in
these routes instead passes the request's connection, so the audit row and the
change it records commit or roll back together. A failed audit write raises out
of ``audit_log`` and aborts the request — that is the intended behavior, not an
edge case to swallow.

Only identifiers are recorded here. No patient identifier, note text or
transcript content is ever passed as an audit field value beyond the resource id
the schema already calls for.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from hipaa_logger import audit_log
from track_a_clinical.db import raw_asyncpg_connection

#: Actions this service records. Values are the audit vocabulary from CLAUDE.md's
#: audit_log schema comment, not free text invented per call site.
ACTION_START_SESSION = "START_SESSION"
ACTION_END_SESSION = "END_SESSION"
ACTION_READ_ENCOUNTER = "READ_ENCOUNTER"

SERVICE_NAME = "track-a-clinical"
RESOURCE_TYPE_ENCOUNTER = "Encounter"


async def audit_encounter_access(
    session: AsyncSession,
    *,
    action: str,
    encounter_id: uuid.UUID,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Record one access to an ``encounters`` row on the caller's transaction.

    Args:
        session: The active session whose transaction the audit row joins.
        action: One of the ``ACTION_*`` constants in this module.
        encounter_id: Primary key of the encounter touched.
        session_id: The encounter's session identifier, for trace correlation.
        provider_id: The acting provider, or None when the caller is anonymous.
        ip_address: Client IP, when the call came from a request.
        user_agent: Client user agent, when the call came from a request.
    """
    await audit_log(
        actor_id=str(provider_id) if provider_id else None,
        action=action,
        resource_type=RESOURCE_TYPE_ENCOUNTER,
        resource_id=str(encounter_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        ip_address=ip_address,
        user_agent=user_agent,
        conn=await raw_asyncpg_connection(session),
    )

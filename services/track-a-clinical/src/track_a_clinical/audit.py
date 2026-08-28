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
#: A re-mint (TASK-006b) reads an encounter and issues a credential for it. Kept
#: distinct from START_SESSION so an audit can tell "a visit was opened" from "a
#: visit's token was refreshed" — they are different events with the same actor.
ACTION_REMINT_SESSION_TOKEN = "REMINT_SESSION_TOKEN"
#: A generated SOAP note was stored (TASK-030). Written by a Redis consumer with
#: no request behind it, which is why the actor comes from the encounter row —
#: see CLAUDE.md "Auditing work that no request triggered".
ACTION_WRITE_NOTE = "WRITE_NOTE"

SERVICE_NAME = "track-a-clinical"
RESOURCE_TYPE_ENCOUNTER = "Encounter"
RESOURCE_TYPE_CLINICAL_NOTE = "ClinicalNote"


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


async def audit_note_write(
    session: AsyncSession,
    *,
    note_id: uuid.UUID,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None,
) -> None:
    """Record that a SOAP note was generated and stored, on the caller's transaction.

    No ``ip_address`` or ``user_agent``: this write is triggered by a Redis
    signal, so there is no client and never will be one. They are permanently
    absent here rather than waiting on the request-context middleware that will
    populate them for routes.

    One row per note, not one per transcript segment. The auditable access is
    the generation — which reads the whole accumulated transcript and produces
    the record — and a row per segment would bury the events an audit is asked
    about under per-message noise.

    Args:
        session: The active session whose transaction the audit row joins.
        note_id: Primary key of the ``clinical_notes`` row written.
        session_id: The encounter's session identifier. The encounter itself is
            not a field here — ``resource_id`` names the note, and the note's
            row carries the encounter it belongs to.
        provider_id: Taken from the ``encounters`` row — the provider who opened
            the visit. Never a service account invented to fill the field.
    """
    await audit_log(
        actor_id=str(provider_id) if provider_id else None,
        action=ACTION_WRITE_NOTE,
        resource_type=RESOURCE_TYPE_CLINICAL_NOTE,
        resource_id=str(note_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        conn=await raw_asyncpg_connection(session),
    )

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

from hipaa_logger import AuditAction, audit_log
from track_a_clinical.db import raw_asyncpg_connection

#: The actions this service records are ``AuditAction`` members, imported from
#: hipaa-logger rather than re-declared here: START_SESSION, END_SESSION,
#: READ_ENCOUNTER, REMINT_SESSION_TOKEN, WRITE_NOTE, READ_NOTE, UPDATE_NOTE,
#: READ_PRIOR_AUTH and SUBMIT_PRIOR_AUTH.
#: A local constant per service is what let the vocabulary drift from its own
#: definition three times — see ``hipaa_logger.actions``.
SERVICE_NAME = "track-a-clinical"
RESOURCE_TYPE_ENCOUNTER = "Encounter"
RESOURCE_TYPE_CLINICAL_NOTE = "ClinicalNote"
RESOURCE_TYPE_PRIOR_AUTH_REQUEST = "PriorAuthRequest"


async def audit_encounter_access(
    session: AsyncSession,
    *,
    action: AuditAction,
    encounter_id: uuid.UUID,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Record one access to an ``encounters`` row on the caller's transaction.

    Args:
        session: The active session whose transaction the audit row joins.
        action: The encounter-scoped ``AuditAction`` this access records.
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
        action=AuditAction.WRITE_NOTE,
        resource_type=RESOURCE_TYPE_CLINICAL_NOTE,
        resource_id=str(note_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        conn=await raw_asyncpg_connection(session),
    )


async def audit_note_access(
    session: AsyncSession,
    *,
    action: AuditAction,
    note_id: uuid.UUID,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Record one request-driven access to a ``clinical_notes`` row (TASK-032).

    The counterpart to :func:`audit_note_write`, and deliberately a separate
    function rather than a flag on it: this one has a client. A note read or
    edited through the review endpoints carries ``ip_address`` and
    ``user_agent``, where the consumer's generation write leaves them
    permanently absent because no request exists for it.

    ``provider_id`` still comes from the ``encounters`` row rather than from
    anything the caller sent. These routes take no credential in v1 — see
    CLAUDE.md, "Session-scoped routes are keyed on ``session_id``" — so the
    provider recorded when the visit was opened is the only defensible actor,
    and the absence of authentication makes this row matter more, not less.

    Args:
        session: The active session whose transaction the audit row joins.
        action: ``AuditAction.READ_NOTE`` or ``AuditAction.UPDATE_NOTE``.
        note_id: Primary key of the ``clinical_notes`` row touched.
        session_id: The encounter's session identifier, for trace correlation.
        provider_id: The provider from the encounter row.
        ip_address: Client IP, from the request.
        user_agent: Client user agent, from the request.
    """
    await audit_log(
        actor_id=str(provider_id) if provider_id else None,
        action=action,
        resource_type=RESOURCE_TYPE_CLINICAL_NOTE,
        resource_id=str(note_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        ip_address=ip_address,
        user_agent=user_agent,
        conn=await raw_asyncpg_connection(session),
    )


async def audit_prior_auth_access(
    session: AsyncSession,
    *,
    action: AuditAction,
    request_id: uuid.UUID,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Record one access to a ``prior_auth_requests`` row (TASK-054).

    The row holds ``clinical_evidence`` — transcript excerpts — so reading one is
    a PHI access as surely as reading a note is, and it audits for the same
    reason. Both callers today are ``fhir-integration`` submitting a request it
    did not assemble: it reads the row to build what it sends, and comes back to
    record what the payer said.

    **Two services write a ``SUBMIT_PRIOR_AUTH`` row for one submission, and that
    is not double counting.** That service records that a request was transmitted
    to a payer; this one records that the row it owns was changed to carry the
    answer. Two distinct accesses, told apart by ``service_name`` — the same
    arrangement the note write-back already uses, and the one CLAUDE.md's
    "Writing clinical data out to the EHR" describes.

    ``provider_id`` comes from the ``encounters`` row the request hangs off,
    never from the calling service: a service-to-service hop does not change
    whose visit this is.

    Args:
        session: The active session whose transaction the audit row joins.
        action: ``AuditAction.READ_PRIOR_AUTH`` or
            ``AuditAction.SUBMIT_PRIOR_AUTH``.
        request_id: Primary key of the ``prior_auth_requests`` row touched.
        session_id: The encounter's session identifier, for trace correlation.
        provider_id: The provider from the encounter row.
        ip_address: Client IP, from the request.
        user_agent: Client user agent, from the request.
    """
    await audit_log(
        actor_id=str(provider_id) if provider_id else None,
        action=action,
        resource_type=RESOURCE_TYPE_PRIOR_AUTH_REQUEST,
        resource_id=str(request_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        ip_address=ip_address,
        user_agent=user_agent,
        conn=await raw_asyncpg_connection(session),
    )

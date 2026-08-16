"""The audit write itself.

Every PHI access in every MedAuth service goes through :func:`audit_log`. The
record holds identifiers only — never note text, transcript content, or patient
demographics. Nothing here writes to stdout: an audit payload must not leak into
process logs.
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Final

import asyncpg

# Imported as a module, not by value: callers and tests replace attributes on
# hipaa_logger.db, and a by-value import would bind past those replacements.
from . import db
from .db import AuditLogError

_INSERT_AUDIT_EVENT: Final[str] = """
    INSERT INTO audit_log (
        actor_id, action, resource_type, resource_id, session_id,
        service_name, request_id, ip_address, user_agent
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""


class InvalidAuditFieldError(AuditLogError, ValueError):
    """Raised when a field cannot be coerced to the column type it targets."""


def _as_uuid(value: str | uuid.UUID | None, field: str) -> uuid.UUID | None:
    """Coerce a UUID-typed field, raising rather than dropping a malformed value."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidAuditFieldError(f"{field} is not a valid UUID: {value!r}") from exc


def _as_ip(
    value: str | ipaddress.IPv4Address | ipaddress.IPv6Address | None,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Coerce the INET-typed ip_address field."""
    if value is None:
        return None
    if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return value
    try:
        return ipaddress.ip_address(value)
    except (TypeError, ValueError) as exc:
        raise InvalidAuditFieldError(f"ip_address is not a valid IP address: {value!r}") from exc


async def audit_log(
    actor_id: str | None,
    action: str,
    resource_type: str | None,
    resource_id: str | None,
    session_id: str | None,
    service_name: str,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    conn: asyncpg.Connection | None = None,
) -> None:
    """Record one PHI access in the ``audit_log`` table.

    Args:
        actor_id: UUID of the provider or service account responsible. None only
            for unauthenticated paths that still touch PHI.
        action: What was done, e.g. ``READ_PATIENT``, ``WRITE_NOTE``.
        resource_type: FHIR or domain resource kind, e.g. ``Patient``.
        resource_id: Identifier of the resource. An identifier only — never content.
        session_id: UUID of the encounter session, when one is in scope.
        service_name: Which service made the call, e.g. ``track-b-rag``.
        request_id: UUID correlating this event to a traced request.
        ip_address: Client IP, when the call originated from a request.
        user_agent: Client user agent, when the call originated from a request.
        conn: Write on this connection instead of the shared pool. Use it to put
            the audit write inside the caller's transaction.

    Raises:
        InvalidAuditFieldError: A UUID or IP field could not be coerced.
        AuditLogError: The database write failed. Never suppressed — an audit
            failure has to stop the operation it was recording.
    """
    if not action:
        raise InvalidAuditFieldError("action is required")
    if not service_name:
        raise InvalidAuditFieldError("service_name is required")

    params = (
        _as_uuid(actor_id, "actor_id"),
        action,
        resource_type,
        resource_id,
        _as_uuid(session_id, "session_id"),
        service_name,
        _as_uuid(request_id, "request_id"),
        _as_ip(ip_address),
        user_agent,
    )

    connection = conn or db.get_injected_connection()
    try:
        if connection is not None:
            await connection.execute(_INSERT_AUDIT_EVENT, *params)
        else:
            pool = await db.get_pool()
            async with pool.acquire() as acquired:
                await acquired.execute(_INSERT_AUDIT_EVENT, *params)
    except AuditLogError:
        raise
    except Exception as exc:  # noqa: BLE001 — every failure mode surfaces as AuditLogError
        raise AuditLogError(
            f"Failed to write audit event {action!r} from {service_name!r}: {exc}"
        ) from exc

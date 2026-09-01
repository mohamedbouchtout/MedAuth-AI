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
from .actions import AuditAction
from .db import AuditLogError

_INSERT_AUDIT_EVENT: Final[str] = """
    INSERT INTO audit_log (
        actor_id, action, resource_type, resource_id, session_id,
        service_name, request_id, ip_address, user_agent,
        fhir_practitioner_ref
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""

#: The ``fhir_practitioner_ref`` column's width. A FHIR reference is normally an
#: absolute URL, so this is wider than ``resource_id``. Enforced here as well as
#: in the schema: asyncpg would surface an over-long value as a database error
#: naming the column, and an audit write that fails should say which field the
#: caller got wrong.
_MAX_FHIR_REFERENCE_LENGTH: Final = 512


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


def _as_action(value: AuditAction | str) -> AuditAction:
    """Coerce the action field to a member of the vocabulary, or refuse the write.

    The signature of :func:`audit_log` asks for an :class:`AuditAction`, so mypy
    catches an invented action at the call site. This is the same check at
    runtime, for the callers static typing does not reach — a test passing a
    literal, or any future untyped path. The column itself is free text and
    would accept anything, which is exactly why the refusal has to happen here.
    """
    if isinstance(value, AuditAction):
        return value
    try:
        return AuditAction(value)
    except ValueError:
        raise InvalidAuditFieldError(
            f"action {value!r} is not in the audit action vocabulary. Add it to "
            "hipaa_logger.AuditAction in the same change as the code that writes it."
        ) from None


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


def _as_fhir_reference(value: str | None) -> str | None:
    """Coerce the EHR-asserted actor field.

    Deliberately not validated against a reference grammar. What this column
    records is what the EHR asserted, verbatim, and a caller has already
    verified the assertion before reaching here — re-parsing it would be this
    package second-guessing a check it did not perform and cannot repeat. The
    only refusals are the two that would corrupt the record rather than merely
    look unusual: a value the column cannot hold, and an empty string, which
    would read as an identity where there is none.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidAuditFieldError(
            f"fhir_practitioner_ref must be a string reference, got {type(value).__name__}"
        )
    stripped = value.strip()
    if not stripped:
        raise InvalidAuditFieldError(
            "fhir_practitioner_ref is empty. Pass None for an unknown or "
            "unverified actor — an empty string reads as an identity."
        )
    if len(stripped) > _MAX_FHIR_REFERENCE_LENGTH:
        raise InvalidAuditFieldError(
            f"fhir_practitioner_ref exceeds {_MAX_FHIR_REFERENCE_LENGTH} characters"
        )
    return stripped


async def audit_log(
    actor_id: str | None,
    action: AuditAction,
    resource_type: str | None,
    resource_id: str | None,
    session_id: str | None,
    service_name: str,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    fhir_practitioner_ref: str | None = None,
    conn: asyncpg.Connection | None = None,
) -> None:
    """Record one PHI access in the ``audit_log`` table.

    Args:
        actor_id: UUID of the provider or service account responsible. None only
            for unauthenticated paths that still touch PHI. Never filled from
            ``fhir_practitioner_ref``: the two are different kinds of
            identifier and neither is a fallback for the other.
        action: What was done, as a member of :class:`~hipaa_logger.AuditAction`.
            A string outside that vocabulary is refused rather than written.
        resource_type: FHIR or domain resource kind, e.g. ``Patient``.
        resource_id: Identifier of the resource. An identifier only — never content.
        session_id: UUID of the encounter session, when one is in scope.
        service_name: Which service made the call, e.g. ``track-b-rag``.
        request_id: UUID correlating this event to a traced request.
        ip_address: Client IP, when the call originated from a request.
        user_agent: Client user agent, when the call originated from a request.
        fhir_practitioner_ref: The actor as an EHR asserted it — a FHIR
            ``Practitioner`` reference, stored verbatim. Pass it **only after
            verifying the assertion**; an unverified or absent claim is None,
            because an unverified identity in an audit trail is a fabrication.
            See CLAUDE.md, "The EHR-asserted actor is its own column".
        conn: Write on this connection instead of the shared pool. Use it to put
            the audit write inside the caller's transaction.

    Raises:
        InvalidAuditFieldError: A UUID or IP field could not be coerced, the
            practitioner reference is empty or too long for its column, or the
            action is not in the vocabulary.
        AuditLogError: The database write failed. Never suppressed — an audit
            failure has to stop the operation it was recording.
    """
    checked_action = _as_action(action)
    if not service_name:
        raise InvalidAuditFieldError("service_name is required")

    params = (
        _as_uuid(actor_id, "actor_id"),
        # str(), so what reaches asyncpg is an ordinary string rather than an
        # enum member. The column is text and reads back as text; nothing
        # downstream should have to know the vocabulary is an enum.
        str(checked_action),
        resource_type,
        resource_id,
        _as_uuid(session_id, "session_id"),
        service_name,
        _as_uuid(request_id, "request_id"),
        _as_ip(ip_address),
        user_agent,
        _as_fhir_reference(fhir_practitioner_ref),
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
            f"Failed to write audit event {str(checked_action)!r} from {service_name!r}: {exc}"
        ) from exc

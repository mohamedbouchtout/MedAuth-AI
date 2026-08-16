"""Shared HIPAA audit logging.

Every PHI access in every MedAuth service is recorded here. This package owns the
``audit_log`` table and its Alembic migration, because every service depends on it
and it cannot wait on a service-owned schema.

Typical use::

    from hipaa_logger import audit_log

    await audit_log(
        actor_id=provider_id,
        action="READ_PATIENT",
        resource_type="Patient",
        resource_id=patient_id,
        session_id=session_id,
        service_name="track-b-rag",
    )

The record holds identifiers only — never PHI content.
"""

from .audit import InvalidAuditFieldError, audit_log
from .db import (
    AuditConfigurationError,
    AuditLogError,
    close_pool,
    configure,
    normalize_dsn,
    set_connection,
    sqlalchemy_dsn,
)

__all__ = [
    "AuditConfigurationError",
    "AuditLogError",
    "InvalidAuditFieldError",
    "audit_log",
    "close_pool",
    "configure",
    "normalize_dsn",
    "set_connection",
    "sqlalchemy_dsn",
]

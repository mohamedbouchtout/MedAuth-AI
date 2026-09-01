"""Audit writes for this service's PHI reads.

These are the first ``audit_log()`` calls in ``fhir-integration``. TASK-051
deferred them deliberately: obtaining a credential is not using it, so the SMART
launch routes write no audit row, and the first PHI access is TASK-052's
resource fetches.

**No connection is passed, unlike ``track_a_clinical.audit``.** That service
hands its request's connection in so the audit row and the change it records
commit together. Nothing here writes to Postgres at all — these routes read from
the EHR — so there is no transaction to join, and ``audit_log()`` uses its own
pool. It reads ``DATABASE_URL`` from the environment itself, which is why this
service's ``Settings`` does not carry one.

**``actor_id`` is ``None`` permanently here, and the actor is recorded in
``fhir_practitioner_ref`` instead.** No ``encounters`` row exists at SMART
launch — the launch precedes the visit — so there is no ``provider_id`` to read
a UUID actor from, and this repository refuses to mint a service-account UUID to
fill a nullable column. What the EHR *does* assert is who authorized the launch,
in the ``id_token``'s ``fhirUser`` claim, and TASK-051c verifies that claim and
stores the resulting ``Practitioner`` reference on the launch record. It cannot
go in ``actor_id``: a ``Practitioner`` id is usually not a UUID and that column
refuses one. See CLAUDE.md, "The EHR-asserted actor is its own column".

**``session_id`` is ``None`` here permanently too, and nothing will ever fill
it.** An earlier version of this module said TASK-051c would fill it alongside
the actor, which was never possible: a SMART launch is not an encounter session,
the two are different identifiers with different lifetimes, and at launch time
no encounter exists to name. See CLAUDE.md, "A SMART launch is not an encounter
session". A later route that genuinely runs inside a visit would pass one; these
do not.

Only identifiers are recorded. No patient name, coverage detail or condition
ever reaches an audit field.
"""

from __future__ import annotations

from hipaa_logger import AuditAction, audit_log

#: The actions this service records are ``AuditAction`` members, imported from
#: hipaa-logger rather than re-declared here: READ_PATIENT and READ_ENCOUNTER.
#: A local constant per service is what let the vocabulary drift from its own
#: definition three times — see ``hipaa_logger.actions``.
SERVICE_NAME = "fhir-integration"
RESOURCE_TYPE_PATIENT = "Patient"
RESOURCE_TYPE_ENCOUNTER = "Encounter"


async def audit_ehr_read(
    *,
    action: AuditAction,
    resource_type: str,
    resource_id: str,
    fhir_practitioner_ref: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Record one read of a patient's data from an EHR.

    **One row per route call, not one per FHIR fetch.** The auditable access is
    the context read a provider asked for; ``get_patient_context()`` happens to
    make three round trips, and a row each would bury the event an audit is
    actually asked about under per-fetch noise. Same "one row per unit of work"
    rule the Redis consumers follow.

    Args:
        action: ``READ_PATIENT`` or ``READ_ENCOUNTER``.
        resource_type: The FHIR resource type read.
        resource_id: The id read, on the EHR that answered.
        fhir_practitioner_ref: The provider who authorized the launch this read
            was made under, from ``get_audit_actor``. ``None`` when the EHR sent
            no ``id_token`` or the token did not verify — an unknown actor, and
            never the unverified claim.
        ip_address: Client IP, when the request context carried one.
        user_agent: Client user agent, when the request context carried one.
    """
    await audit_log(
        # A UUID actor and a session are both structurally absent here, not
        # pending: no encounters row exists at launch time. The EHR-asserted
        # actor goes in its own column. Never fill either with an invented value.
        actor_id=None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        session_id=None,
        service_name=SERVICE_NAME,
        ip_address=ip_address,
        user_agent=user_agent,
        fhir_practitioner_ref=fhir_practitioner_ref,
    )

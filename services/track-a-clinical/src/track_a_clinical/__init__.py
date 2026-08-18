"""track-a-clinical service.

Owns SOAP note generation and, from TASK-005 onward, the Alembic migration
history for the platform's core schema. The mapped classes live in
:mod:`track_a_clinical.models` and are imported by every service that writes
one of those tables — ``track-b-rag`` writes ``clinical_nudges``, ``prior-auth``
writes ``prior_auth_requests`` — so there is exactly one definition of each
table in the monorepo.
"""

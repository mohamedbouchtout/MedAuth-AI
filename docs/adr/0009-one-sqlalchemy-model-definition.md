# ADR-0009: The shared tables have one model definition, in track-a-clinical

**Status:** Accepted · **Task:** TASK-005

## Context

Five core tables — `encounters`, `clinical_notes`, `clinical_nudges`,
`prior_auth_requests`, `insurance_policies` — are migrated by `track-a-clinical`
but written by several services. `clinical_nudges` is written by `track-b-rag`;
`prior_auth_requests` by `prior-auth`.

The obvious arrangement is for each service to map its own SQLAlchemy class
against the tables it uses. That produces two or more class definitions of one
table, which drift from each other and from the migration history, and nothing
catches the drift until a write fails in production.

## Decision

The mapped classes live in
`services/track-a-clinical/src/track_a_clinical/models/`, one module per table,
exported from the package `__init__`. Every service that touches those tables
imports from there:

```python
from track_a_clinical.models import ClinicalNudge, Encounter
```

**Migration ownership is not write access.** "track-a-clinical owns the schema"
means it owns the Alembic history for those tables. Every service connects to
the same Postgres via `DATABASE_URL` and reads and writes freely; only migration
authorship is centralised.

## Consequences

- This is why `track-a-clinical` builds `src/track_a_clinical/` rather than a
  bare `src/`: a service whose code is imported across a boundary needs a real
  package name. `track-b-rag` was renamed for the same reason at TASK-010, one
  task before TASK-011 needed to import these models.
- A service depending on these models declares `medauth-track-a-clinical` as a
  dependency, which is a build-time coupling between services that are otherwise
  independent at runtime. Accepted: the alternative is silent schema drift.
- A schema change is one edit and one migration, and every consumer sees it.

## References

- `services/track-a-clinical/src/track_a_clinical/models/`
- `CLAUDE.md` -> Where the shared SQLAlchemy models live; Migration Ownership vs. Table Write Access

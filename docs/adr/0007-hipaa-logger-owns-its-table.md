# ADR-0007: hipaa-logger owns its own table and writes it with raw asyncpg

**Status:** Accepted · **Task:** TASK-002

## Context

Every service depends on `hipaa-logger`, and every service therefore needs the
`audit_log` table to exist before its own first PHI-touching route runs. If the
table belonged to `track-a-clinical`'s migration history, the package would be
unusable until that service's migrations had been applied — a package depending
on a service that depends on the package.

Separately, the rest of the backend uses SQLAlchemy 2.0 async for its domain
models, and consistency argues for using it here too.

## Decision

Two things, both deliberate departures from the surrounding conventions.

1. **`hipaa-logger` owns `audit_log` and its own Alembic history**, in
   `packages/hipaa-logger/migrations/`. It is applied first, before any
   service-owned migration.
2. **The insert is raw `asyncpg`, not SQLAlchemy.** This is a single hot-path
   `INSERT` with nine scalar parameters. An ORM buys nothing here and costs a
   session, a mapper and an import of the service model layer that a package
   under every service has no business pulling in.

The connection is a self-managed lazy pool built from `DATABASE_URL`, with an
explicit injection hook: `set_connection(conn)`, and an optional `conn`
parameter on `audit_log()`. That hook is what lets tests mock the write, and
what lets a service that needs the audit row inside its own transaction pass its
connection in.

Because `DATABASE_URL` is written in SQLAlchemy dialect form
(`postgresql+asyncpg://`), which raw asyncpg cannot parse, the package strips
the driver suffix on connect rather than requiring a second environment variable.

## Consequences

- SQLAlchemy 2.0 async remains the standard for services with real domain
  models. This is an intentional exception, documented as one, not an
  inconsistency to be tidied away later.
- One `DATABASE_URL` works for every consumer in the monorepo; no service needs
  to know which driver style another package expects.
- The audit write can participate in a caller's transaction when correctness
  demands it, and be a fire-and-forget pool write when it does not.

## References

- `packages/hipaa-logger/src/hipaa_logger/audit.py`, `db.py`
- `packages/hipaa-logger/migrations/versions/0001_create_audit_log.py`
- `CLAUDE.md` -> DATABASE_URL format

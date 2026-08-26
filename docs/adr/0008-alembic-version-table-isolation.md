# ADR-0008: Every Alembic setup gets its own version table

**Status:** Accepted · **Task:** TASK-002 / TASK-005

## Context

`hipaa-logger` and `track-a-clinical` each own an Alembic migration history, and
both run against the same Postgres database. Alembic records the current head
revision in a table named `alembic_version` by default.

Two Alembic setups sharing that default table read each other's revision id as
their own head. The failure is not a clean error: each setup concludes it is
already at a revision it has never seen, skips migrations that were never
applied, and corrupts migration state in a way that only surfaces when a query
hits a column that does not exist.

## Decision

Every package or service with its own Alembic setup sets a unique
`version_table` in its `migrations/env.py`, named
`alembic_version_{package_or_service_name_with_underscores}`.

```python
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    version_table="alembic_version_hipaa_logger",
)
```

This applies to every future Alembic setup, not only the two that exist.

## Consequences

- Each history is independently upgradeable and downgradeable.
- The naming pattern is mechanical, so a new setup has no judgement to exercise
  and no way to collide.
- Adding a new Alembic setup means remembering this. It is stated in `CLAUDE.md`
  as a rule for that reason.

## References

- `packages/hipaa-logger/migrations/env.py`
- `services/track-a-clinical/migrations/env.py`
- `CLAUDE.md` -> Alembic version table isolation

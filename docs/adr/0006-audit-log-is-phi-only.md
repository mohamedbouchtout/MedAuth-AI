# ADR-0006: `audit_log` records PHI access and nothing else

**Status:** Accepted · **Task:** TASK-002; clarified during TASK-011

## Context

An earlier phrasing of the rule required an `audit_log()` call on **every**
route. It immediately needed a carve-out for `GET /health`, and was about to
need a second for `POST /policies/ingest`, which writes public payer documents
with no patient linkage. A rule accumulating exceptions is the wrong rule.

The `audit_log` table's value comes from every row in it being a PHI access.
Mix operational writes in and "who accessed patient X" stops being a query you
can simply run and becomes one you have to filter — and a filter that has to be
correct is a filter that will eventually be wrong.

## Decision

A route calls `audit_log()` **if and only if** it touches PHI. The rule binds in
both directions.

- A route over PHI **must** audit.
- A route over public or operational data **must not**. Health and liveness
  probes touch no PHI, and auditing a Kubernetes probe on its polling interval
  is noise. `POST /policies/ingest` writes public payer publications.
- Those routes log at INFO through `logging.getLogger(__name__)` instead, which
  still produces the operational trace, in the right place.

The distinction is decided per route and stated in the code, not guessed. Where
a query reads a PHI-bearing table but selects only non-PHI columns — as
`policy_dispatch` does against `encounters` — the columns are named explicitly
in the SELECT so that the decision holds under review, and a later
`select(Encounter)` would visibly change it.

## Consequences

- "Who accessed patient X" is `SELECT * FROM audit_log WHERE ...` with no
  predicate distinguishing real accesses from probe traffic.
- Each new route requires an explicit judgement, which is the point.
- `hipaa-logger` is not a general application logger and must not grow into one.
  It has one function, one table, and one purpose.

## References

- `packages/hipaa-logger/src/hipaa_logger/audit.py`
- `services/track-b-rag/src/track_b_rag/policy_dispatch.py`
- `CLAUDE.md` -> packages/hipaa-logger — Design Decisions
- `TASKS.md` -> Known Constraints #6

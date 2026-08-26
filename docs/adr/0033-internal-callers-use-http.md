# ADR-0033: Internal callers reach `/policies/query` over HTTP

**Status:** Accepted · **Task:** TASK-021

## Context

The transcript consumer needs to run a policy query. It lives in the **same
process** as the `/policies/query` route, so it could simply call
`query.answer_policy_query()` directly — no serialisation, no loopback, no
timeout to configure.

The `audit_log()` write for `/policies/query` lives in the **route layer**,
because what has to be recorded is that a PHI-carrying request was made on
behalf of a particular provider for a particular session.

Calling the service function directly would skip that write. Moving the audit
down into `answer_policy_query()` so both paths were covered would put the
compliance obligation in two places, and two hand-maintained copies of an
obligation drift.

## Decision

The transcript consumer posts to `POST /policies/query` over HTTP like any other
caller, against `POLICY_QUERY_BASE_URL` with a `POLICY_QUERY_TIMEOUT_SECONDS`
bound.

**One call path, one audit site.**

## Consequences

- Every PHI-carrying policy query produces exactly one audit row, written in
  exactly one place, regardless of who asked.
- The cost is a loopback HTTP round trip per query. Against a Qdrant search and
  a Sonnet call it is not measurable.
- `POLICY_QUERY_BASE_URL` is configurable because the loopback address differs
  between a laptop, a pod and a test.
- The 15s timeout bounds a hung connection rather than expressing a latency
  target — a query taking that long has already missed the moment it was meant
  to inform.
- This generalises: an internal caller of any PHI route calls it over HTTP
  rather than importing its service function.

## References

- `services/track-b-rag/src/track_b_rag/policy_dispatch.py`

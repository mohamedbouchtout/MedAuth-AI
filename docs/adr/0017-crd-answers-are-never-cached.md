# ADR-0017: A CRD answer is never cached

**Status:** Accepted · **Task:** TASK-015

## Context

`/policies/query` already caches its expensive half for 24 hours under
`rag:{payer}:{plan_type}:{state}:{cpt_code}` (ADR-0014). A CRD determination is
keyed on exactly the same four values, so writing it into the same entry is the
obvious extension, and it would remove a live HTTP call from the hot path.

## Decision

The CRD path **neither reads nor writes** the `rag:` key.

Mechanically, this is enforced by function decomposition rather than by a rule
someone has to remember: `_resolve_policy_tier()` performs the cache read and
the cache write, and `resolve_policy_rules()` applies the CRD determination to
the value that function returns. The determination is applied *after* the cache
write, so what Redis holds is always the payer-policy answer and never a live
determination.

## Consequences

- The two kinds of answer are cached according to what they are. A RAG answer
  is an interpretation of a published document that changes on the order of
  months; a day of staleness is an accepted trade for a Qdrant search and a
  Sonnet call. A CRD answer's entire value over RAG is being live and
  authoritative from the payer's own system at the moment of the order. Caching
  it for 24 hours would leave a slower, more complex way to get a stale answer.
- Every query for a covered payer makes a live CRD call, bounded at 4 seconds.
- If CRD latency ever threatens the nudge budget, the answer is a short
  seconds-scale TTL of its own — **never** the 24h payer-policy key.

## References

- `services/track-b-rag/src/track_b_rag/policy_rules.py` (`_resolve_policy_tier`, `_apply_determination`)
- `CLAUDE.md` -> A CRD answer is never cached; a RAG answer is

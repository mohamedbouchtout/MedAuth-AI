# ADR-0032: Gap analysis is deterministic Python, not a second model call

**Status:** Accepted · **Task:** TASK-012

## Context

Stage 2 compares the payer's criteria against what this encounter has documented
so far, and produces `missing_criteria`, `denial_risk` and `nudge_message`.

The accurate way to decide whether "documented failure of six weeks of
conservative therapy" is satisfied by a given clinical note is to ask a model.

But Stage 2 runs on **every** request, including the cache hits — which are most
of them. A model call here would put the expensive thing back into the path that
ADR-0014 exists to make cheap, and would add its latency to every nudge.

## Decision

Stage 2 is deterministic Python. Every input it needs is already in memory once
Stage 1 has answered.

The matcher is an explicit **term-overlap heuristic**: criterion terms are
tokenised, stopwords removed, and a criterion counts as documented when at least
`CRITERION_COVERAGE_THRESHOLD` (0.6) of its terms appear in the encounter's
vocabulary. `denial_risk` is a function of how many criteria are missing out of
how many exist, with a floor applied when step therapy is required.

It is not natural language understanding, and it is the part of this module most
likely to be replaced. Two properties matter more than its precision:

- **It is deterministic**, so its output is reproducible in a test rather than
  something to be asserted loosely. This is the only reason TASK-012's "no
  Bedrock call on a cache hit" test can be written as an equality rather than a
  hope.
- **It errs toward reporting a criterion as missing.** A criterion wrongly
  listed as missing costs a provider a glance at a nudge; a criterion wrongly
  treated as satisfied is a silent gap in a prior authorization — the failure
  direction ADR-0015 rules out.

## Consequences

- The cache's cost saving is intact and per-nudge latency stays inside what a
  live encounter tolerates.
- Precision is limited by term overlap. A note that documents a criterion in
  entirely different words is reported as missing. Accepted, in that direction.
- **No PHI leaves the module.** The clinical context is read here and referenced
  nowhere in the output: `missing_criteria` echoes the payer's own criteria text,
  and `nudge_message` is built from those criteria and the procedure name.
- `nudge_message` names at most `NUDGE_CRITERIA_LIMIT` (3) criteria, because a
  banner listing nine is a banner nobody reads.

## References

- `services/track-b-rag/src/track_b_rag/gap_analysis.py`

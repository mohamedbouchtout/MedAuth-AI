# ADR-0015: An unresolvable query fails toward "authorization required"

**Status:** Accepted · **Task:** TASK-012

## Context

The RAG path has many ways to not produce an answer: nothing indexed for this
payer, an unreachable Qdrant, a Bedrock error, a model response that is not
JSON, a response that is JSON of the wrong shape.

Each of those could reasonably return a 5xx. But the consumer is the transcript
consumer, which fires nudges during a live encounter, and in that setting
**silence reads as "nothing to worry about."** A provider who sees no banner
concludes the order is clear.

The two failure directions are not symmetric. Wrongly saying authorization is
required costs a provider one unnecessary check. Wrongly implying it is not
costs a patient a denied claim and the practice an appeal.

## Decision

Every failure of the RAG path collapses to one fixed, safe answer rather than an
error:

```
requires_auth      = True
auth_criteria      = []
missing_criteria   = []
denial_risk        = "high"
nudge_message      = "Unable to verify authorization requirements — confirm manually"
```

The message is verbatim from TASK-012. It says exactly what happened — the
requirements could not be established — and asks for a manual check rather than
implying an answer in either direction.

The same principle appears throughout the path:

- The CRD tier reads `conditional` on `pa-needed` as *required*, because a payer
  saying "maybe" must not become "no".
- **Silence from a payer is never "no authorization required."** An empty card
  list, a documentation-only card, and an "unable to process" card all mean *no
  determination*, and the RAG path answers alone.
- Redis dedup fails **open**: a failed claim is treated as a first mention, so
  the provider may see one duplicate nudge rather than none at all.
- The CPT resolver refuses rather than guesses (ADR-0031).
- Gap analysis errs toward reporting a criterion as missing (ADR-0032).

## Consequences

- `/policies/query` returns 200 with the safe answer rather than 5xx. Callers
  never have to decide what an error means mid-encounter.
- A fallback is never cached.
- The service will produce some nudges that were not necessary. That is the
  chosen direction and should not be "tuned down" without revisiting this record.
- The distinction between "we hold no policy for this payer" and "the payer name
  did not line up" is preserved by a WARNING log, because from the outside those
  two look identical. See ADR-0022.

## References

- `services/track-b-rag/src/track_b_rag/query.py` (`fallback_answer`, `FALLBACK_NUDGE_MESSAGE`)
- `services/track-b-rag/src/track_b_rag/policy_rules.py` (`FALLBACK_RULES`)

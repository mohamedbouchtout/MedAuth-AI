# ADR-0014: The policy answer splits into a cacheable payer half and an uncached patient half

**Status:** Accepted · **Task:** TASK-012

## Context

A cache miss on `/policies/query` costs a Qdrant vector search plus a Claude
Sonnet call over the retrieved policy text. That is the dominant cost of the
whole product, and it repeats for every patient with the same payer, plan and
procedure — which, in a practice, is most of them. Caching it is the single
biggest cost lever in the system.

The natural cache key is `rag:{payer}:{plan_type}:{state}:{cpt_code}`, and the
natural thing to store under it is the response. But the response mixes two
kinds of data:

| Field | Describes |
|---|---|
| `requires_auth`, `auth_criteria`, `step_therapy_required`, `step_therapy_details` | the payer's policy |
| `missing_criteria`, `denial_risk`, `nudge_message` | *this patient's documentation* |

Caching the second group under a key that does not mention the patient serves
patient B the documentation gaps computed for patient A. That is a
patient-safety defect, not a stale-cache annoyance.

Adding `clinical_context` to the key would be correct and would collapse the hit
rate to near zero, defeating the point.

## Decision

The query runs as two stages, and the split is enforced by the type signatures
rather than by convention.

**Stage 1 — `policy_rules.resolve_policy_rules()`.** Retrieval plus one Sonnet
call. It produces the payer-policy fields and *takes no clinical context
parameter at all*, so nothing patient-specific can reach the prompt, the
retrieved passages or the cached value. Its result is cached for 24 hours under
the payer-scoped key.

**Stage 2 — `gap_analysis.assess()`.** Compares Stage 1's criteria against this
encounter's documentation. Produces the patient-specific fields. Never cached,
runs on every call including cache hits.

Nothing in `cache.py` accepts anything but a serialised `PolicyRules`.

## Consequences

- The expensive half is paid once per payer/plan/state/procedure per day; the
  cheap half runs every time. The full cost benefit is preserved.
- Two patients with the same plan and procedure get the same criteria and their
  own gaps, which is exactly right.
- **When Stage 1 falls back, Stage 2 does not run at all.** A fallback means the
  criteria are unknown, so there is nothing to compare a note against, and a
  computed `missing_criteria` of `[]` would read as "nothing is missing" — the
  false reassurance the fallback exists to prevent. See ADR-0015.
- A fallback is never written to the cache: it records what one call failed to
  learn, not what the payer requires.
- The 24h TTL bounds how long a withdrawn policy can keep answering. Payer
  policies move on the order of months and the nightly scraper re-ingests them,
  so a day-old answer is current.

## References

- `services/track-b-rag/src/track_b_rag/query.py`, `policy_rules.py`, `gap_analysis.py`, `cache.py`
- `CLAUDE.md` -> Key Architectural Constraints (cache note)

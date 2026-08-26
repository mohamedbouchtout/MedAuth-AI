# Design: Real-Time Policy Lookup

**Service:** `services/track-b-rag` · **Tasks:** TASK-010 to TASK-016, TASK-021,
TASK-024

This is the core of the product. Everything else in the platform exists to
deliver a transcript to this service and a nudge back out of it.

## The question being answered

A clinician says *"let's get an MRI of that knee."* Within seconds, before the
conversation moves on, the system must answer:

1. Does this patient's payer require prior authorization for this procedure?
2. What does the payer require to be documented?
3. What has this encounter **not** documented yet?
4. How likely is a denial, and what should the banner say?

Questions 1 and 2 are about the payer and are identical for every patient on that
plan. Questions 3 and 4 are about *this* patient. **That distinction is the
architecture.**

## The pipeline

```
transcript segment
     |
     v
keywords.detect_procedures()          fixed keyword list + 2 sentences of context
     |                                 ADR-0030
     v
procedure_codes.resolve()             spoken phrase -> CPT, or a typed refusal
     |                                 ADR-0031
     v
dedup.claim_procedure()               SADD; first mention wins, atomically
     |                                 ADR-0029
     v
policy_dispatch.resolve_and_query()   encounter -> query params -> HTTP POST
     |                                 ADR-0033
     v
POST /policies/query
     |
     +---- Stage 1: policy_rules.resolve_policy_rules()      CACHED 24h
     |         |
     |         +-- cache.get_cached()          rag:{payer}:{plan}:{state}:{cpt}
     |         +-- retrieval.retrieve()        embed + Qdrant, TOP_K=8
     |         +-- bedrock (Sonnet)            one call, one retry
     |         +-- crd.determine()             concurrent, never cached
     |                                          ADR-0014, 0016, 0017
     v
     +---- Stage 2: gap_analysis.assess()                    NEVER CACHED
               deterministic term overlap        ADR-0032
     |
     v
PolicyQueryAnswer -> nudge
```

## Stage 1 — what the payer requires

`resolve_policy_rules()` produces four fields: `requires_auth`, `auth_criteria`,
`step_therapy_required`, `step_therapy_details`.

**It takes no clinical context parameter.** That is the enforcement mechanism,
not a convention: nothing patient-specific can reach the prompt, the retrieved
passages or the cached value, because the function has nowhere to put it.

### The RAG tier

1. **Cache read** at `rag:{payer}:{plan_type}:{state}:{cpt_code}`. A hit that
   will not parse is discarded and treated as a miss — a cache that can poison a
   request is worse than no cache.
2. **Embed the query.** The text is built as
   *"Prior authorization requirements for CPT 73721 (MRI) under an aetna PPO
   plan."* The CPT code leads because it is what the answer is really about;
   the spoken procedure name is included because a bare code embeds poorly — the
   indexed policy text says "magnetic resonance imaging of the lumbar spine" far
   more often than it says "72148".
3. **Search Qdrant**, `TOP_K = 8`, filtered to this payer and to policies that
   apply in this state or nationally.
4. **One Sonnet call** over the retrieved passages, prompted to answer for the
   CPT code and to treat the clinician's wording as a hint about intent only.
   The model is told to say authorization is required and return an empty
   criteria list rather than guessing where the passages do not establish an
   answer.
5. **One retry** on a malformed response, with the same prompt — a malformed
   answer is treated as a sampling accident rather than a prompt defect. A second
   failure is a failure of the path.
6. **Cache write** on success.

The response model uses `extra="ignore"` rather than `forbid`: an answer carrying
the four fields plus a chatty `notes` key is a usable answer, and spending the
single retry on it would trade a correct result for a fallback.

### The CRD tier

For payers covered by the CMS-0057-F mandate, the payer's own Da Vinci CRD
endpoint runs **concurrently** with the RAG tier and decides `requires_auth`.
It supplies nothing else — the IG's `ext-coverage-information` extension carries
no criterion text at all, which was established by running the Reference
Implementation and then reading the StructureDefinition
([ADR-0016](../adr/0016-two-tier-crd-and-rag.md)).

Two response dialects are read: the spec-conformant `pa-needed` slice, and the
Reference Implementation's `coverageInfo` slice with the determination in the
card *type*. The conformant signal is checked first.

The determination is applied **after** the cache write, which is what
mechanically guarantees a live answer never lands in Redis
([ADR-0017](../adr/0017-crd-answers-are-never-cached.md)).

### Provenance

Every resolution reports where it came from, because with CRD results uncached
"the CRD tier answered" can no longer be inferred from cache state:

| `source` | Meaning |
|---|---|
| `cache` | Served from Redis. No Bedrock call was made. |
| `rag` | Full path ran: retrieval plus one Sonnet call. |
| `fallback` | The path failed. The safe default is being returned. |
| `crd` | CRD answered where the policy tier had nothing. Criteria list is empty. |
| `crd+cache` | CRD supplied `requires_auth`; criteria came from the cache. |
| `crd+rag` | CRD supplied `requires_auth`; criteria came from a fresh RAG run. |

`source` is not part of the HTTP response. It is for the log line and for the
tests that assert a cache hit did not reach Bedrock.

## Stage 2 — what this encounter is missing

`gap_analysis.assess()` produces `missing_criteria`, `denial_risk` and
`nudge_message`, and is **never cached**.

It is deterministic Python, not a second model call
([ADR-0032](../adr/0032-deterministic-gap-analysis.md)). A criterion counts as
documented when at least 60% of its non-stopword terms appear in the encounter's
vocabulary. `denial_risk` is a function of missing-over-total, with a floor
applied where step therapy is required. `nudge_message` names at most three
criteria, because a banner listing nine is a banner nobody reads.

The matcher is the part most likely to be replaced. Two properties matter more
than its precision: it is **deterministic**, and it **errs toward reporting a
criterion as missing**.

## When Stage 1 falls back, Stage 2 does not run

A fallback means the payer's criteria are unknown. There is nothing to compare a
note against, and a computed `missing_criteria` of `[]` would read as "nothing is
missing" — precisely the false reassurance the fallback exists to prevent.

The fallback response is fixed:

```
requires_auth    = true
auth_criteria    = []
missing_criteria = []
denial_risk      = "high"
nudge_message    = "Unable to verify authorization requirements — confirm manually"
```

Every RAG failure mode collapses to it: nothing indexed for this payer, an
unreachable Qdrant, a Bedrock error, an answer that is not JSON, and an answer
that is JSON of the wrong shape. The alternative — a 5xx to a consumer firing
nudges during a live encounter — would produce silence, and silence reads as
"nothing to worry about"
([ADR-0015](../adr/0015-fail-toward-authorization-required.md)).

A fallback is never cached: it records what one call failed to learn, not what
the payer requires.

## Ingestion

`POST /policies/ingest` takes a document and its metadata, and performs a
three-way dedup keyed on `(policy_id, content_hash)`:

| State | Action | Reported |
|---|---|---|
| No row for this `policy_id` | Index, insert | `created` |
| Row exists, digest matches | Nothing | `unchanged` |
| Row exists, digest differs | Re-index, update | `updated` |

**Qdrant is written before Postgres**, and the order is load-bearing: a crash in
between leaves a stale `content_hash`, which the next scrape detects and repairs.
Reversed, the row would claim to be current while its vectors were missing, and
nothing would ever retry ([ADR-0020](../adr/0020-qdrant-written-before-postgres.md)).

Ingest writes **no audit row** — policy documents are public payer publications
([ADR-0006](../adr/0006-audit-log-is-phi-only.md)).

## Payer identity

`payer` is matched by exact string equality in two places — the Qdrant filter and
the cache key — so it is a **canonical slug** everywhere, from
`packages/payer-vocab`, never a display name. An unrecognised payer still queries
and logs at WARNING, so "the name did not line up" stays distinguishable from "we
hold no policy for this payer" ([ADR-0022](../adr/0022-canonical-payer-slugs.md)).

## What is not built yet

- **Nudge emission** (TASK-040). The query answers; nothing yet publishes to
  `nudges:{session_id}`.
- **Keyword-only nudges** (TASK-044) for procedures that resolve no CPT code.
- **The blocking gap:** `resolve_query_parameters()` raises on every real
  encounter because `encounters.insurance_payer`, `insurance_plan_type` and
  `state` are nullable and nothing populates them. That is **TASK-052b**, gated
  on the SMART launch. A placeholder was ruled out — a made-up value files a real
  policy answer under a key standing for a different plan, and unrelated
  encounters then collide on it.
- **A certified coder's review** of the CPT table is a prerequisite on TASK-052b,
  because that is the task after which its codes can reach a provider.

## Tuning constants and where they come from

| Constant | Value | Basis |
|---|---|---|
| `TOP_K` | 8 | Enough passages that a criteria list spread across a policy's sections survives chunking; few enough to bound prompt cost per miss |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 800 / 150 | Overlap keeps a criterion straddling a boundary retrievable from either side |
| `POLICY_RULES_TTL_SECONDS` | 86400 | Payer policies move on the order of months; bounds how long a withdrawn policy keeps answering |
| `MAX_ATTEMPTS` | 2 | One retry — a malformed answer is a sampling accident, not a prompt defect |
| `CRITERION_COVERAGE_THRESHOLD` | 0.6 | Heuristic. Round-number default, not empirically derived |
| `NUDGE_CRITERIA_LIMIT` | 3 | A banner listing nine is a banner nobody reads |
| `CRD_TIMEOUT_SECONDS` | 4.0 | **Measured**: ~0.5s steady state, ~3.0s cold start compiling CQL rule libraries |
| `PROCEDURE_SEEN_TTL_SECONDS` | 14400 | Safety net for a visit that never ends, not the mechanism |
| `POLICY_QUERY_TIMEOUT_SECONDS` | 15.0 | Bounds a hung connection; not a latency target |

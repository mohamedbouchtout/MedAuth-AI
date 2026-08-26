# Data Model

Three stores, each holding a different kind of thing, plus one deliberate
absence. This document is descriptive: the Alembic migrations are authoritative
for Postgres, and `CLAUDE.md`'s canonical key list is authoritative for Redis.

---

## PostgreSQL

One database, shared by every service. **Migration ownership is not write
access** — `track-a-clinical` owns the Alembic history for the five core tables,
but any service reads and writes them via the shared SQLAlchemy models
([ADR-0009](../adr/0009-one-sqlalchemy-model-definition.md)).

Conventions that apply to every table:

- UUID primary keys generated **server-side** via `gen_random_uuid()`, never by
  a client.
- **Soft deletes** — a `deleted_at TIMESTAMPTZ` column, never a hard `DELETE`.
- All timestamps are `TIMESTAMPTZ`, stored and returned as ISO 8601 UTC.
- Migrations only. Tables are never altered by hand.

### Migration histories

| Owner | Version table | Migrations |
|---|---|---|
| `packages/hipaa-logger` | `alembic_version_hipaa_logger` | `0001_create_audit_log` |
| `services/track-a-clinical` | `alembic_version_track_a_clinical` | `0001_create_core_schema`, `0002_policy_jurisdiction_states`, `0003_encounter_state` |

Each history gets its own version table. Sharing the default `alembic_version`
would make each setup read the other's revision as its own head and corrupt
migration state ([ADR-0008](../adr/0008-alembic-version-table-isolation.md)).
hipaa-logger's migration is applied **first**, because every service depends on
that package.

### `encounters` — the root of the schema

One row per clinical visit. Notes, nudges and prior-auth requests all hang off it.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `session_id` | UUID, **unique** | The identifier that travels through every Redis channel name and every session JWT. Unique rather than merely indexed: two encounters sharing a session would make every `transcription:{session_id}` subscriber ambiguous. |
| `ehr_encounter_id` | VARCHAR(100) | |
| `patient_fhir_id` | VARCHAR(100), NOT NULL | **PHI.** The wire field `patient_id` on `/sessions/start` maps here. |
| `provider_id` | UUID, NOT NULL | |
| `organization_id` | UUID | |
| `status` | VARCHAR | `active` / `completed` |
| `started_at`, `ended_at` | TIMESTAMPTZ | |
| `insurance_payer` | VARCHAR(200) | **Nullable, and nothing populates it yet** |
| `insurance_plan_type` | VARCHAR(100) | Same |
| `insurance_member_id` | VARCHAR(100) | **PHI** |
| `state` | CHAR(2) | Added in `0003`. Same — see below |
| `deleted_at` | TIMESTAMPTZ | |

Indexes: `idx_encounters_session`, `idx_encounters_provider`.

> **The one blocking gap in the system.** `insurance_payer`,
> `insurance_plan_type` and `state` are filled from a FHIR `Coverage` resource at
> SMART launch, which is **TASK-052b**, gated on TASK-051 and TASK-052. Until
> then `resolve_query_parameters()` raises on every real encounter, naming the
> three fields genuinely absent for *that* encounter. Placeholder values were
> ruled out: the cache key is `rag:{payer}:{plan_type}:{state}:{cpt_code}`, so a
> made-up value files a real policy answer under a key standing for a different
> plan, and unrelated encounters then collide on it.

### `clinical_notes`

`encounter_id` FK, the four SOAP sections as `TEXT`, `icd10_codes` and
`cpt_codes` as `JSONB`, `ehr_document_ref_id`, status, timestamps, `deleted_at`.
Written by track-a-clinical (TASK-030, planned).

### `clinical_nudges`

`encounter_id` FK, `procedure_name`, `cpt_code`, `nudge_message`,
`missing_criteria` (JSONB), `denial_risk`, `payer_policy_source`, `created_at`,
`acknowledged_at`, `resulted_in_documentation`.

Migrated by track-a-clinical, **written by track-b-rag**. The last two columns
are the product's own feedback loop: whether the provider saw the nudge and
whether it changed the documentation.

### `prior_auth_requests`

`encounter_id` FK, `status`, `payer_name`, `procedures` / `diagnoses` /
`clinical_evidence` (JSONB), `submission_method`, `payer_reference_number`,
`submitted_at`, `decided_at`, `denial_reason`.

Migrated by track-a-clinical, **written by prior-auth**. Indexed on
`encounter_id` and `status`.

### `insurance_policies`

Metadata for one ingested policy document. Not PHI.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `payer` | VARCHAR(200), NOT NULL | A **canonical slug**, never a display name ([ADR-0022](../adr/0022-canonical-payer-slugs.md)) |
| `plan_type` | VARCHAR(100) | |
| `state` | CHAR(2) | Single-state or national policies |
| `jurisdiction_states` | TEXT[] | Added in `0002`. A multi-state policy is **one row with a list**, never one row per state ([ADR-0023](../adr/0023-usps-jurisdictions-multi-state-policies.md)) |
| `policy_id` | VARCHAR(200), **unique** | The dedup key, with `content_hash` |
| `source_url` | TEXT | |
| `content_hash` | VARCHAR(64), NOT NULL | SHA-256 over the **raw uploaded bytes** ([ADR-0021](../adr/0021-digest-over-uploaded-bytes.md)) |
| `last_ingested_at` | TIMESTAMPTZ | |
| `effective_date` | DATE | |
| `qdrant_collection` | VARCHAR(100) | Defaults to `insurance_policies` |

Indexed on `(payer, state)`.

### `audit_log`

Owned by `packages/hipaa-logger`, applied before everything else.

```sql
CREATE TABLE audit_log (
    id            BIGSERIAL PRIMARY KEY,
    actor_id      UUID,                  -- provider or service account
    action        VARCHAR(100) NOT NULL, -- READ_PATIENT, WRITE_NOTE, ...
    resource_type VARCHAR(100),
    resource_id   VARCHAR(200),
    session_id    UUID,
    service_name  VARCHAR(100) NOT NULL, -- which service wrote the row
    request_id    UUID,                  -- correlates to request tracing
    ip_address    INET,
    user_agent    TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_audit_log_occurred_at ON audit_log(occurred_at);
CREATE INDEX idx_audit_log_actor       ON audit_log(actor_id);
CREATE INDEX idx_audit_log_session     ON audit_log(session_id);
```

`service_name` and `request_id` go beyond the original architecture sketch:
every service calls this package, so knowing which one wrote each row and being
able to trace it to a request is worth two columns.

**Every row in this table is a PHI access**, and that is what makes "who
accessed patient X" a query you can simply run
([ADR-0006](../adr/0006-audit-log-is-phi-only.md)). `ip_address` and `user_agent`
are real parameters that default to `None` until request-context middleware
populates them — not permanently-empty columns.

### `DATABASE_URL` — one variable, two consumers

CI and `.env.example` set it in SQLAlchemy dialect form,
`postgresql+asyncpg://user:pass@host/db`. SQLAlchemy services use it directly.
Raw asyncpg cannot parse the `+asyncpg` suffix, so hipaa-logger strips it
defensively on connect rather than requiring a second variable. One value works
for every consumer; no service needs to know which driver style another expects.

---

## Redis

Lowercase, colon-separated, most-specific segment last. **This list is
canonical** — a task needing a new pattern adds it here and to `CLAUDE.md` in
the same PR rather than inventing a variant.

### Pub/sub channels

| Channel | Payload | Published by | Consumed by |
|---|---|---|---|
| `sessions:started` | `{"session_id": ...}` | track-a-clinical | track-b-rag |
| `transcription:{session_id}` | Transcript segment | audio-ingestion | track-a-clinical, track-b-rag |
| `nudges:{session_id}` | Nudge event | track-b-rag *(planned)* | nudge-service *(planned)* |
| `session:ended:{session_id}` | *empty* — a signal, not a carrier | track-a-clinical | track-a-clinical, track-b-rag, prior-auth |

`sessions:started` is the one fixed channel and the only one with a payload,
because the channel name has no room for the id. It exists so a consumer can
subscribe to one session's transcript channel **by name**; the alternative was
pattern-subscribing `transcription:*`, a wildcard over the one channel family
carrying speech ([ADR-0028](../adr/0028-per-session-subscription.md)).

### Keys

| Key | Type / TTL | Holds |
|---|---|---|
| `rag:{payer}:{plan_type}:{state}:{cpt_code}` | cache, 24h | **Payer-policy fields only** |
| `procedure_seen:{session_id}` | set, 4h | Procedure keys already queried this encounter |
| `fhir_session:{state_param}` | cache, ~10 min | Transient SMART launch state *(TASK-051)* |
| `fhir_token:{session_id}` | cache, token expiry | EHR access token, FHIR base URL, EHR type *(TASK-051)* |

**`rag:` holds four fields and only four:** `requires_auth`, `auth_criteria`,
`step_therapy_required`, `step_therapy_details`. Never `missing_criteria`,
`denial_risk` or `nudge_message` — those describe *this patient's documentation*
and are recomputed on every call. Caching them under a key that does not mention
the patient would serve patient B the gaps computed for patient A
([ADR-0014](../adr/0014-two-stage-policy-answer.md)). The split is enforced by
the type signatures in `cache.py`, which accept nothing but a serialised
`PolicyRules`.

The `{payer}` segment is a canonical slug, never a display name — a raw name
there silently halves the hit rate and hides a retrieval miss
([ADR-0022](../adr/0022-canonical-payer-slugs.md)).

**`procedure_seen:` members** are `cpt:{code}` where a code resolves and
`keyword:{keyword}` where none does, so two keywords naming one procedure share
a claim — a knee MRI and a hip MRI are both `73721` and are one order. Claimed
with `SADD`, which reports first-add atomically; deleted on `session:ended`, with
the TTL only bounding a visit that never ends
([ADR-0029](../adr/0029-one-nudge-per-procedure-per-encounter.md)).

---

## Qdrant

One collection, `insurance_policies`, holding chunked payer policy text.

| | |
|---|---|
| **Vectors** | 1024 dimensions, cosine distance |
| **Embedder** | `BAAI/bge-large-en-v1.5`, run locally |
| **Chunking** | 800 characters, 150 overlap |
| **Payload fields** | `policy_id`, `payer`, `plan_type`, `state`, `chunk_index`, `text` |
| **Keyword indexes** | `policy_id`, `payer`, `state` |
| **Point ids** | Deterministic UUID5 from `(policy_id, chunk_index)` under a fixed namespace |
| **Retrieval depth** | `TOP_K = 8` |

The 150-character overlap is what keeps a criterion straddling a boundary —
*"...documented failure of six weeks of | conservative therapy..."* — retrievable
from either side. Prior authorization criteria are exactly the kind of sentence
that gets split.

The keyword indexes are why the retrieval filter stays a filter rather than
degrading to a scan as the collection grows with each nightly scrape.

`state` may be a **list** (`["MA", "ME", "NY"]`). Qdrant's `MatchValue` matches
any element of a list-valued payload field, verified against the running Qdrant
with the exact production filter, so multi-state policies need no change to
retrieval ([ADR-0023](../adr/0023-usps-jurisdictions-multi-state-policies.md)).

### The retrieval filter

```python
Filter(
    must=[FieldCondition(key="payer", match=MatchValue(value=payer))],
    should=[
        FieldCondition(key="state", match=MatchValue(value=state)),
        IsNullCondition(is_null=PayloadField(key="state")),
    ],
)
```

Read as: *this payer, and either this state or no state at all.* A policy
ingested with no state applies nationally — CMS national coverage determinations
are the obvious case — and a plain equality filter would hide every one of them
from a query that named a state.

**The collection is never recreated at startup.** `recreate_collection()` drops
and rebuilds, which would silently wipe every indexed policy on each restart,
rollout and pod reschedule ([ADR-0019](../adr/0019-qdrant-get-or-create.md)).

---

## The deliberate absence

**Encounter audio is not stored anywhere.** No bucket, no table, no file. It
exists in a bounded in-memory buffer on the client and another in
`audio-ingestion`, and is discarded as soon as it has been forwarded to
Transcribe Medical ([ADR-0005](../adr/0005-audio-never-persists.md)).

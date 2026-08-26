# Service and Package Catalog

What each unit is, what it depends on, and how much of it exists. Status here
reflects the code on disk; `TASKS.md` is authoritative for task-level detail.

**Legend:** ● implemented · ◐ partially implemented · ○ scaffold only

---

## Services

All seven are Python 3.12 / FastAPI members of the one uv workspace
([ADR-0002](../adr/0002-one-python-uv-workspace.md)).

### ● track-a-clinical — port 8003

Owns session lifecycle and the core database schema.

| | |
|---|---|
| **Built** | `POST /sessions/start`, `POST /sessions/{id}/end`, the session JWT minter, the five shared SQLAlchemy models, three Alembic migrations |
| **Planned** | Transcript accumulation and SOAP generation (TASK-030), Comprehend Medical validation (TASK-031), note review (TASK-032) |
| **Owns migrations for** | `encounters`, `clinical_notes`, `clinical_nudges`, `prior_auth_requests`, `insurance_policies` |
| **Depends on** | Postgres, Redis, `hipaa-logger`, `api-envelope` |
| **Notable** | The **only** minter of session JWTs in the monorepo ([ADR-0012](../adr/0012-single-session-jwt-issuer.md)). Builds `src/track_a_clinical/` rather than a bare `src/` because other services import its models ([ADR-0009](../adr/0009-one-sqlalchemy-model-definition.md)). |

### ● audio-ingestion — port 8001

Terminates the audio WebSocket and streams to AWS Transcribe Medical.

| | |
|---|---|
| **Built** | `WebSocket /ws/audio/{session_id}`, `GET /health`, JWT validation from both carriers, the in-memory audio buffer, the Transcribe Medical client, the Redis publisher |
| **Depends on** | Redis, AWS Transcribe Medical, `hipaa-logger`, `api-envelope` |
| **Notable** | Holds all audio in one module so [ADR-0005](../adr/0005-audio-never-persists.md) is verifiable by reading one file. Reaches Transcribe *Medical* by subclassing the AWS SDK, with the dependency pinned to `==0.6.4` ([ADR-0026](../adr/0026-transcribe-medical-by-sdk-subclass.md)). |

### ● track-b-rag — port 8002

The insurance policy RAG service. The technical moat lives here.

| | |
|---|---|
| **Built** | `POST /policies/ingest`, `POST /policies/query`, `GET /health`, the two-stage query engine, the Da Vinci CRD tier, the Redis policy cache, Qdrant collection management, keyword detection, CPT resolution, the transcript consumer, per-encounter dedup |
| **Planned** | Nudge emission to `nudges:{session_id}` (TASK-040), keyword-only nudges (TASK-044) |
| **Writes** | `clinical_nudges`, `insurance_policies` (migrated by track-a-clinical) |
| **Depends on** | Qdrant, Redis, Postgres, AWS Bedrock (Sonnet), a CRD endpoint, `payer-vocab`, `hipaa-logger`, `api-envelope`, `track-a-clinical` (models) |
| **Notable** | 23 modules and ~5,500 lines — by far the largest service. See [design/rag-policy-lookup.md](../design/rag-policy-lookup.md). |

### ● policy-scraper — CronJob, no port

The nightly CMS coverage-policy ingest. A one-shot process, not a service:
`python -m policy_scraper`.

| | |
|---|---|
| **Built** | Bulk MCD export reader, code-to-LCD selection, jurisdiction resolution, document assembly, robots.txt matcher, rate-limited fetcher, upload to `/policies/ingest` |
| **Depends on** | `downloads.cms.gov`, `www.cms.gov`, track-b-rag, Postgres, `payer-vocab` |
| **Notable** | Makes exactly **three** HTTP requests per run ([ADR-0024](../adr/0024-scraper-reads-bulk-exports.md)). Never chunks, embeds or writes Qdrant itself. Exits non-zero on any failure so Kubernetes marks the job failed. Manifest at `infrastructure/kubernetes/policy-scraper-cronjob.yaml`. |

### ○ fhir-integration — port 8004

SMART on FHIR OAuth and FHIR R4 read/write, behind an EHR adapter layer.

| | |
|---|---|
| **Built** | Package scaffold only |
| **Planned** | Adapter scaffold (TASK-050), SMART OAuth (TASK-051), base resource fetching (TASK-052), encounter payer/plan/state population (TASK-052b), note write-back (TASK-053), prior auth submission (TASK-054), five vendor adapters (TASK-055 to TASK-058) |
| **Notable** | **TASK-052b is the current blocker on the whole nudge path** — `encounters.insurance_payer`, `insurance_plan_type` and `state` are nullable columns that nothing populates yet, so `resolve_query_parameters()` still raises on every real encounter. |

### ○ nudge-service — port 8005

Redis pub/sub to WebSocket relay.

| | |
|---|---|
| **Built** | Package scaffold only |
| **Planned** | The relay (TASK-041), acknowledge endpoint (TASK-041b) |
| **Notable** | Its socket inherits the two-carrier token rule by reference rather than re-deciding it ([ADR-0013](../adr/0013-two-websocket-token-carriers.md)). |

### ○ prior-auth — no port yet

Prior authorization bundle assembly and submission.

| | |
|---|---|
| **Built** | Package scaffold only |
| **Planned** | Bundle assembler (TASK-060), submission router (TASK-061) |
| **Writes** | `prior_auth_requests` (migrated by track-a-clinical) |
| **Notable** | Subscribes to `session:ended:{session_id}`. When it needs CPT resolution, that is the trigger to move the procedure code table into `packages/procedure-codes` ([ADR-0031](../adr/0031-cpt-resolver-refuses-rather-than-guesses.md)). |

---

## Packages

Every package gets its own CI path filter, its own test job, and the same 80%
coverage gate as a service. A change under `packages/` re-runs every dependent
service *and* the package's own suite.

### ● hipaa-logger (Python)

One function, one table, one purpose: a compliance audit row per PHI access.

Owns `audit_log` and its own Alembic history, applied before any service-owned
migration. Raw asyncpg rather than SQLAlchemy
([ADR-0007](../adr/0007-hipaa-logger-owns-its-table.md)). **Not** a general
application logger, and must not become one
([ADR-0006](../adr/0006-audit-log-is-phi-only.md)).

### ● api-envelope (Python)

The single definition of the HTTP response envelope plus FastAPI's error
handlers. Extracted at TASK-010 when track-b-rag became the second consumer.
No routes, no auth, no middleware
([ADR-0010](../adr/0010-single-response-envelope-package.md)).

### ● crypto-utils (Python)

Field-level AES-256-GCM with a KMS-wrapped DEK per record. Encryption context is
bound both at KMS and as GCM AAD
([ADR-0011](../adr/0011-encryption-context-bound-twice.md)). All KMS mocking uses
moto's `@mock_aws`.

### ● payer-vocab (Python)

Canonical payer slugs and USPS jurisdiction normalisation
([ADR-0022](../adr/0022-canonical-payer-slugs.md),
[ADR-0023](../adr/0023-usps-jurisdictions-multi-state-policies.md)). Consumers
span ingest, query, the scraper, the seed script and — from Phase 5 —
fhir-integration.

### ● fhir-types (Python + TypeScript)

FHIR R4 type definitions for Patient, Encounter, Condition, Coverage,
MedicationRequest, DocumentReference and Claim. The **only** package with two
languages in one CI job: it runs pytest against the Pydantic models *and*
`tsc --noEmit` against the TypeScript mirrors, plus a parity test that fails when
the two drift.

### ● audio-wire (TypeScript)

The encounter-audio wire format, the PCM framer and the float-to-int16
conversion, shared by both clients
([ADR-0036](../adr/0036-audio-wire-format-package.md)). Ships source rather than
a built artefact, so a change here sets the `web` and `mobile` CI filters too.

---

## Applications

### ◐ apps/web — React 19 + Vite + Tailwind

Built: `useAudioCapture`, the AudioWorklet processor, config. Planned: the SMART
on FHIR launch, nudge UI (TASK-042), session management (TASK-070), note review
(TASK-071), the prior auth dashboard (TASK-072).

React **19**, not 18: `apps/mobile` is already on 19.2.x through Expo SDK 57, and
two React majors in one npm workspace root is a cost with nothing to buy — there
was no existing web code to migrate. Zustand is not installed until a task has
state to keep in it.

Capture is `getUserMedia` → `AudioContext` → `AudioWorkletNode`, never
MediaRecorder ([ADR-0034](../adr/0034-browser-capture-audioworklet.md)).

### ◐ apps/mobile — React Native, Expo SDK 57

Built: `useAudioCapture`, config. Planned: session screen (TASK-025), haptic
nudge (TASK-043), capture deadline (TASK-026).

Capture is `expo-audio`'s `useAudioStream`, never `expo-av`
([ADR-0035](../adr/0035-mobile-capture-expo-audio.md)).

---

## Local ports

| Port | What |
|---|---|
| 8001 | audio-ingestion |
| 8002 | track-b-rag |
| 8003 | track-a-clinical |
| 8004 | fhir-integration |
| 8005 | nudge-service |
| 8006 | Da Vinci CRD Reference Implementation |
| 8080 | HAPI FHIR (synthetic EHR) |
| 5432 | PostgreSQL |
| 6333 | Qdrant (REST); 6334 gRPC |
| 6379 | Redis |

The CRD container listens on 8090 internally and is published as **8006**
because Windows reserves the 8081–8180 range and the container cannot bind 8090
there.

# MedAuth AI — Claude Code Context

## What This Project Is
Ambient clinical AI platform for healthcare providers. Listens to physician-patient
encounters in real time, generates SOAP notes, queries insurance payer policies via RAG,
and fires live alerts ("nudges") when a procedure order is missing prior authorization
criteria. Integrates with hospital EHR systems via SMART on FHIR.

**The core technical moat:** real-time RAG against insurance policy databases during
the clinical encounter — this does not exist in any current product.

## Regulatory Context (Read Before Writing Any Code)
- This system processes Protected Health Information (PHI) — HIPAA applies to everything
- Never log PHI to stdout or any unencrypted store
- Never write audio data to disk — process in memory only, then discard
- Every PHI access must be written to the audit_log table via the hipaa-logger package
- All secrets go in AWS Secrets Manager — never in code or .env files committed to git
- TLS everywhere — no plaintext HTTP internally or externally

## Monorepo Structure
```
medauth-ai/
├── apps/
│   ├── web/              # React + TypeScript, SMART on FHIR launch
│   └── mobile/           # React Native (Expo)
├── services/
│   ├── audio-ingestion/  # FastAPI + WebSocket, streams to AWS Transcribe Medical
│   ├── track-a-clinical/ # SOAP note generation — Claude via AWS Bedrock
│   ├── track-b-rag/      # Insurance policy RAG — Qdrant + Claude via Bedrock
│   ├── fhir-integration/ # SMART on FHIR OAuth + FHIR R4 read/write
│   ├── prior-auth/       # Prior authorization bundle assembly + submission
│   ├── nudge-service/    # Redis pub/sub → WebSocket relay to clients
│   └── policy-scraper/   # Nightly insurance policy PDF ingestion CronJob
├── packages/
│   ├── hipaa-logger/     # Shared audit logging — every service imports this
│   ├── fhir-types/       # Shared FHIR R4 type definitions (Python + TypeScript)
│   └── crypto-utils/     # AES-256 helpers used across services
├── infrastructure/
│   ├── terraform/        # AWS infrastructure as code
│   └── kubernetes/       # K8s manifests + Helm chart
├── scripts/
│   ├── seed-synthea.sh   # Load synthetic patients into local HAPI FHIR (stub until TASK-052)
│   └── setup-dev.sh      # One-command dev environment setup (stub until TASK-052)
└── docker-compose.yml    # Full local stack — postgres, redis, qdrant, hapi-fhir
```

## Tech Stack

### Python Services (audio-ingestion, track-a-clinical, track-b-rag, prior-auth, nudge-service, policy-scraper)
- **Runtime:** Python 3.12
- **Package manager:** uv (NOT pip, NOT poetry)
- **Web framework:** FastAPI with uvicorn
- **Async:** asyncio throughout — no sync blocking calls in async contexts
- **LLM:** Claude via AWS Bedrock (model: `anthropic.claude-haiku-4-5-20251001` for fast tasks, `anthropic.claude-sonnet-4-6` for SOAP/RAG reasoning)
- **LLM orchestration:** LangChain + LangChain-AWS
- **Vector store:** Qdrant (self-hosted) via qdrant-client
- **Embeddings:** sentence-transformers `BAAI/bge-large-en-v1.5` (local, no external API)
- **Medical NLP:** boto3 Comprehend Medical
- **Message bus:** Redis pub/sub (NOT Kafka — added when >20 providers)
- **Database:** PostgreSQL via asyncpg + SQLAlchemy 2.0 (async)
- **Testing:** pytest + pytest-asyncio, httpx for async client testing

### fhir-integration service
- **Runtime:** Python 3.12 (same as all other services — one uv workspace)
- **Framework:** FastAPI with uvicorn
- **FHIR client:** fhirclient (pip package)
- **Testing:** pytest + pytest-asyncio + httpx
- Note: earlier drafts mentioned Node.js for this service — that is incorrect.
  Python was chosen to keep the entire backend in one uv workspace with
  consistent tooling, testing, and deployment patterns.

### Frontend (apps/web)
- **Framework:** React 18 + TypeScript
- **Build:** Vite
- **Styling:** Tailwind CSS
- **State:** Zustand
- **FHIR:** fhirclient.js for SMART on FHIR OAuth flow
- **WebSocket:** native WebSocket API (no socket.io)
- **Testing:** Vitest + React Testing Library

### Frontend (apps/mobile)
- **Framework:** React Native with Expo SDK 51
- **Audio:** expo-av for capture
- **Testing:** Jest + React Native Testing Library

### Infrastructure
- **Cloud:** AWS (us-east-1) — all HIPAA-eligible services, BAA signed
- **Containers:** Docker + EKS (Kubernetes)
- **IaC:** Terraform
- **CI/CD:** GitHub Actions

## Local Development

### Prerequisites
```bash
# Required
docker & docker compose
uv (pip install uv)
node 20+ & npm
aws cli (configured with dev credentials)
```

### Start full local stack
```bash
docker compose up          # Postgres, Redis, Qdrant, HAPI FHIR server
./scripts/seed-synthea.sh  # Load 100 synthetic patients into HAPI FHIR
./scripts/setup-dev.sh     # Install all Python + Node dependencies
```

### Run a single service locally
```bash
cd services/track-b-rag
uv run uvicorn src.main:app --reload --port 8002
```

### Local service ports
```
8080  HAPI FHIR (synthetic EHR)
8001  audio-ingestion
8002  track-b-rag
8003  track-a-clinical
8004  fhir-integration
8005  nudge-service
5432  PostgreSQL
6379  Redis
6333  Qdrant
```

### Environment variables for local dev
Copy `.env.example` to `.env.local`. Never commit `.env.local`.
For local dev, AWS credentials use the `medauth-dev` IAM profile.
Bedrock is the only AWS service called during local dev (no local mock available).

## Code Conventions

### Python
- Type hints on every function signature — no bare `Any` unless unavoidable
- Pydantic v2 models for all request/response schemas and config
- No print() statements — use `logging.getLogger(__name__)`
- Async-first: `async def` for all route handlers and service methods
- Every function that touches PHI must call `await audit_log(...)` from hipaa-logger
- 100-character line length (configured in pyproject.toml)
- Docstrings on all public functions

### TypeScript
- Strict mode enabled — no `any`, use `unknown` and narrow
- Named exports only (no default exports in shared packages)
- Interfaces for data shapes, type aliases for unions
- Errors bubble up as typed Result objects, not thrown exceptions

### API Design
- REST for all service-to-service communication
- OpenAPI spec lives in `docs/api/<service-name>.yaml` — update it when adding routes
- All endpoints return `{"data": ..., "error": null}` or `{"data": null, "error": {...}}`
- Pagination via `?cursor=` (cursor-based, not offset)
- All timestamps in ISO 8601 UTC

### Database
- Migrations via Alembic — never alter tables manually
- New migration: `cd services/<name> && uv run alembic revision --autogenerate -m "description"`
- All UUIDs generated server-side (gen_random_uuid()), never client-side
- Soft deletes — add `deleted_at TIMESTAMPTZ` column, never hard DELETE

### Testing
- Unit tests for all business logic (pure functions, no external calls)
- Integration tests for all API routes using test database and mocked AWS services
- Moto for mocking AWS services (Bedrock, Transcribe Medical, Comprehend Medical)
- Test files mirror src structure: `src/services/rag.py` → `tests/unit/services/test_rag.py`
- Minimum 80% coverage on services/packages; CI fails below this

### Git Commits
- **50/72 rule.** Subject line 50 characters or fewer; body hard-wrapped at 72
  columns. The subject must still carry the type, scope, and task number, so keep
  it terse and put the explanation in the body.
- Subject is imperative mood, no trailing period.
- Blank line between subject and body. Trailers (`Co-Authored-By:`) go last.
- **Every commit must pass CI on its own**, not just the tip of the branch. Order
  the work so tooling and config land before the code that depends on them, and
  squash or reorder any "fixes the previous commit" commit before opening a PR —
  history has to stay bisectable.
- One logical change per commit. A branch that mixes CI changes, docs, and feature
  code should be three commits, not one.

```
feat(hipaa-logger): add audit_log [TASK-002]
<blank>
Body explains why, wrapped at 72 columns. What changed is visible in
the diff; the body is for the reasoning that is not.
<blank>
Co-Authored-By: ...
Authored-By: ...
```

## Key Architectural Constraints
- **Audio never persists.** Process in-memory BytesIO buffers only. Discard immediately after transcription.
- **Claude is called via AWS Bedrock only** (not Anthropic's direct API). This is the HIPAA-eligible path.
- **No Kafka until >20 providers.** Redis pub/sub for now. The service interfaces are identical so swapping later is a config change.
- **Qdrant for vector store.** Do not use Pinecone or Weaviate — we self-host for PHI control even though insurance policy text is not PHI (defense in depth).
- **Cache RAG results in Redis** with 24h TTL keyed by `rag:{payer}:{plan_type}:{state}:{cpt_code}`. This is a major cost lever — implement it from the start.
- **Haiku for extraction tasks, Sonnet for reasoning.** ICD/CPT entity extraction → Haiku. SOAP generation and payer policy analysis → Sonnet. Costs 15x less per extraction call.
- **Policy lookup is two-tier, not RAG-only.** For payers covered by the CMS-0057-F
  mandate (Medicare Advantage, Medicaid managed care, CHIP, ACA marketplace),
  `/policies/query` (TASK-012) tries the Da Vinci CRD/DTR standardized API first
  (TASK-015) and only falls back to the RAG/Qdrant/Sonnet path (TASK-010–014) on
  failure or for unsupported payers. Commercial employer-sponsored plans — the
  bulk of what private practices see — are not covered by the mandate and stay
  on the RAG path for the foreseeable future. Both paths return the same response
  shape; callers never branch on which one answered.

### Session Lifecycle & JWT Issuance (read before Phase 1/2/3/4 tasks)
This is the one piece every real-time service depends on but nothing explicitly
creates until now — Phase 2's audio-ingestion task validates a "session JWT" that
otherwise has no issuer. Fixed here:
- `services/track-a-clinical` owns session lifecycle, because it owns the
  `encounters` table (TASK-005).
- `POST /sessions/start` — body: `{patient_id, provider_id, ehr_encounter_id}`.
  Creates an `encounters` row (`status='active'`, wire field `patient_id` maps
  to the model's `patient_fhir_id` column), mints a short-lived JWT with claims
  `{session_id, provider_id, exp}` — no `iss`/`aud` claims for v1, even though
  `.env.example` has `JWT_ISSUER`/`JWT_AUDIENCE` vars sitting unused from an
  earlier scaffold pass. Adding them means TASK-020's and TASK-041's validators
  grow too — defer that to a later hardening task rather than expanding the
  claim set here. Lifetime is driven by `SESSION_TTL_SECONDS` (default 900,
  i.e. 15 min) — not a hardcoded literal. `session_id` is generated server-side
  (UUID), never client-supplied, per the UUID convention in Code Conventions.
  Returns `{session_id, jwt}`. This is what apps/web and apps/mobile call when
  a provider taps "start visit."
- `POST /sessions/{session_id}/end` — sets `encounters.status='completed'`,
  `ended_at=NOW()`, and publishes `session:ended:{session_id}` to Redis
  (empty payload — it's a signal, not a data carrier). This is the trigger
  TASK-030 (SOAP generation) and TASK-060 (prior auth bundle assembly) both
  subscribe to. Semantics: unknown or soft-deleted `session_id` → 404.
  Repeat-ending an already-completed session → 200, idempotent, does NOT
  publish a second Redis signal (TASK-030 and TASK-060 both react to that
  signal — a duplicate publish would trigger duplicate SOAP generation or
  duplicate bundle assembly, so idempotency here isn't just tidiness).
- The JWT is what `audio-ingestion` (TASK-020) and `nudge-service` (TASK-041)
  validate before accepting a WebSocket connection. Signing secret is
  `JWT_SIGNING_KEY` in `.env.example` — symmetric (HS256) is fine for v1,
  every service is first-party.
- This session-start/session-end pair did not exist as its own task in earlier
  drafts of this file — it is now **TASK-006**, added to Phase 0, and it is a
  prerequisite for TASK-020, TASK-021, TASK-030, TASK-041, and TASK-060. Do not
  start those without TASK-006 done first.

### Redis Key Naming — Canonical List
Every task below should use these exact patterns, not invent variants:
```
transcription:{session_id}      pub/sub — raw transcript segments, published by
                                 audio-ingestion (TASK-020), consumed by
                                 track-a-clinical (TASK-030) and track-b-rag (TASK-021)
nudges:{session_id}              pub/sub — nudge events, published by track-b-rag
                                 (TASK-040), consumed by nudge-service (TASK-041)
session:ended:{session_id}       pub/sub — empty-payload signal, published by
                                 track-a-clinical (TASK-006), consumed by
                                 track-a-clinical itself (TASK-030) and
                                 prior-auth (TASK-060)
rag:{payer}:{plan_type}:{state}:{cpt_code}
                                  cache, 24h TTL — policy query results
                                  (TASK-012)
fhir_session:{state_param}       cache, TTL = OAuth flow timeout (~10 min) —
                                  transient SMART launch state (TASK-051)
fhir_token:{session_id}          cache, TTL = token expiry — EHR access token
                                  + fhir_base_url + ehr_type (TASK-051)
```
Lowercase, colon-separated, most-specific segment last. If a task needs a new
Redis key pattern not listed here, add it to this list in the same PR.

### Qdrant Initialization — Must Be Idempotent
`qdrant.recreate_collection()` deletes and rebuilds the collection — calling it
on every service startup would wipe all indexed insurance policies every time
`track-b-rag` restarts. Use a get-or-create pattern instead:
```python
from qdrant_client.http.exceptions import UnexpectedResponse


def ensure_collection(client: QdrantClient, name: str, vector_size: int):
    try:
        client.get_collection(name)
    except UnexpectedResponse:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
```
`recreate_collection` is acceptable only in a one-off dev reset script that a
human runs deliberately — never in application startup code.

### Bedrock Model Assignment (concrete, per call site)
The "Haiku for extraction, Sonnet for reasoning" rule above, made specific:
| Call site | Model | Why |
|---|---|---|
| TASK-012 policy query analysis | Sonnet | Multi-step reasoning over retrieved policy text |
| TASK-030 SOAP note generation | Sonnet | Long-form structured clinical writing |
| TASK-030 ICD-10/CPT extraction (LLM pass) | Haiku | Extraction, not reasoning — validated against Comprehend Medical in TASK-031 anyway |
| TASK-013 policy scraper (if any LLM cleanup used) | Haiku | Simple text cleanup, not analysis |
Reference `BEDROCK_MODEL_SONNET` / `BEDROCK_MODEL_HAIKU` from `.env.example` —
never hardcode a model ID string in application code.

### Migration Ownership vs. Table Write Access (clarifies TASK-005)
"Owns the schema" means owns the Alembic migration history for those tables —
it does not mean only that service may read/write them. `clinical_nudges` is
migrated by track-a-clinical but written by track-b-rag; `prior_auth_requests`
is migrated by track-a-clinical but written by prior-auth. Every service
connects to the same Postgres instance via `DATABASE_URL` and uses SQLAlchemy
models generated from the same schema — only migration authorship is centralized,
not query access.

### Where the shared SQLAlchemy models live (cross-cutting — applies to every task)
The mapped classes for the five core tables live in
`services/track-a-clinical/src/track_a_clinical/models/`, one module per table,
exported from the package `__init__`. Every service that touches those tables
imports from there rather than mapping its own class against the same table:

```python
from track_a_clinical.models import ClinicalNudge, Encounter
```

A second definition of a table drifts away from the migration history and from
the first definition, and nothing catches it until a write fails in production.
TASK-006, TASK-030, TASK-040 and TASK-060 all write these tables and all import
these classes.

That import path is why `services/track-a-clinical` builds
`src/track_a_clinical/` rather than a bare `src/`, the way `packages/*` already
do. Every other service still declares `packages = ["src"]` in its
`pyproject.toml`, so they all install a top-level module named `src` and shadow
each other in the shared venv — `import src.models` resolves to whichever
service sorts first. That is tolerable while nothing imports across service
boundaries, and each of those services should move to a named package as it
grows code worth importing.

### packages/hipaa-logger — Design Decisions (locked, do not revisit)
**Scope note (read first):** this package is NOT a general application logger.
It writes one specific thing — a compliance audit trail row per PHI access —
to the audit_log Postgres table. It does not replace standard Python `logging`,
does not handle debug/info/error output, and is not used for anything except
PHI access events. Normal application logging (service startup, errors, request
traces) uses `logging.getLogger(__name__)` per the Python conventions section
above and goes to stdout/CloudWatch, not this package. Neither one ever logs
actual PHI content — hipaa-logger records metadata about an access (who, what
resource type, when), never the patient data itself.
- **Owns its own audit_log table and Alembic migration.** Every service depends on
  this package, so it cannot wait on another service's schema (see TASK-002/TASK-005
  ordering note below). The migration lives in `packages/hipaa-logger/migrations/`
  and is applied first, before any service-owned migration.
- **Raw asyncpg, not SQLAlchemy.** Single hot-path INSERT — an ORM adds nothing here.
  SQLAlchemy 2.0 async remains standard for services with real domain models
  (track-a-clinical, prior-auth, etc.) — this is an intentional exception, not
  an inconsistency.
- **Self-managed lazy connection pool**, initialized from `DATABASE_URL` on first use.
  Provides an explicit injection hook (`set_connection(conn)` / accepts an optional
  `conn` param on `audit_log()`) so tests can mock it and so services that need the
  audit write inside their own transaction can pass their connection in.

### audit_log table schema (authoritative — matches architecture doc)
```sql
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    actor_id UUID,                  -- provider or service account
    action VARCHAR(100) NOT NULL,   -- e.g. READ_PATIENT, WRITE_NOTE, SUBMIT_PRIOR_AUTH
    resource_type VARCHAR(100),     -- e.g. Patient, Encounter, ClinicalNote
    resource_id VARCHAR(200),
    session_id UUID,
    service_name VARCHAR(100) NOT NULL,   -- which service made the call, e.g. "track-b-rag"
    request_id UUID,                      -- correlates to request tracing, nullable
    ip_address INET,
    user_agent TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_audit_log_occurred_at ON audit_log(occurred_at);
CREATE INDEX idx_audit_log_actor ON audit_log(actor_id);
CREATE INDEX idx_audit_log_session ON audit_log(session_id);
```
`service_name` and `request_id` were added beyond the original architecture doc
sketch — every service calls this package, so knowing which one wrote each row
and being able to trace it to a specific request is worth the two extra columns.

`audit_log()` function signature:
```python
async def audit_log(
    actor_id: str | None,
    action: str,
    resource_type: str | None,
    resource_id: str | None,
    session_id: str | None,
    service_name: str,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    conn: asyncpg.Connection | None = None,  # injection hook — uses pool if omitted
) -> None: ...
```
`ip_address` and `user_agent` are optional and default to None until a request-context
mechanism (likely FastAPI middleware) populates them automatically in a later task.
Ship them as real parameters, not permanently-empty columns silently filled with NULL.

### Alembic version table isolation
hipaa-logger's migrations and each service's migrations run against the same database.
If two Alembic setups share the default `alembic_version` table, they read each other's
revision as their own head and corrupt migration state. Every package/service with its
own Alembic setup must set a unique `version_table` in its `env.py`:
```python
# packages/hipaa-logger/migrations/env.py
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    version_table="alembic_version_hipaa_logger",
)
```
```python
# services/track-a-clinical/migrations/env.py
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    version_table="alembic_version_track_a_clinical",
)
```
Pattern: `alembic_version_{package_or_service_name_with_underscores}`. Apply this to
every future Alembic setup, not just these two.

### DATABASE_URL format — single env var, two consumers
CI and .env.example set `DATABASE_URL` in SQLAlchemy dialect form:
`postgresql+asyncpg://user:pass@host/db`. SQLAlchemy services use this directly.
Raw asyncpg (hipaa-logger) cannot parse the `+asyncpg` driver suffix, so hipaa-logger
strips it defensively on connect rather than requiring a second env var:
```python
def _to_asyncpg_dsn(database_url: str) -> str:
    """SQLAlchemy-style URLs use postgresql+asyncpg://; raw asyncpg wants postgresql://"""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
```
One `DATABASE_URL` value works for every consumer in the monorepo — services never
need to know which driver style another package expects.


- FHIR version: R4 (4.0.1)
- SMART on FHIR version: 2.0
- Local dev FHIR server: HAPI FHIR at localhost:8080 (Docker)
- FHIR resources used: Patient, Encounter, Condition, Coverage, MedicationRequest, DocumentReference, Claim

### EHR Priority Order (do not deviate from this)
1. **Athenahealth** — build and certify first. Most accessible developer program,
   common in private orthopedic and dermatology practices (our target customers).
   Sandbox: developer.athenahealth.com
2. **eClinicalWorks** — second. Large presence in specialty practices.
   Sandbox: developer.eclinicalworks.com
3. **Modernizing Medicine (EMA)** — third. Specifically targets dermatology and
   orthopedics — our exact specialties. Higher priority than market share suggests.
4. **Cerner (Oracle Health)** — fourth. Faster certification than Epic.
   Sandbox: code.cerner.com
5. **Epic** — last. Largest market but hardest certification (6-12 months,
   requires reference customer). Pursue only after paying customers on other EHRs.
   Sandbox: fhir.epic.com

### Adapter Architecture (mandatory — do not build single-EHR)
All EHR integration goes through an adapter layer in services/fhir-integration.
Never write EHR-specific logic directly into route handlers or other services.

```
services/fhir-integration/src/adapters/
├── base.py        # EHRAdapter — standard FHIR R4 / US Core, works on all EHRs
├── athena.py      # AthenaAdapter(EHRAdapter) — overrides prior auth (no FHIR PAS)
├── ecw.py         # ECWAdapter(EHRAdapter) — minor coverage field handling
├── modmed.py      # ModMedAdapter(EHRAdapter) — EMA-specific extensions
├── cerner.py      # CernerAdapter(EHRAdapter) — coverage fallback handling
├── epic.py        # EpicAdapter(EHRAdapter) — proprietary extension enrichment
└── factory.py     # get_adapter(ehr_type, fhir_base_url, access_token) -> EHRAdapter
                   # detect_ehr_from_issuer(iss_url) -> str
```

The SMART launch `iss` parameter identifies the EHR vendor. Pass it to
`detect_ehr_from_issuer()` to get the right adapter — never hardcode EHR type.

What lives in base.py (standard FHIR, same across all EHRs):
- get_patient_context() — Patient + Coverage + Condition resources
- write_clinical_note() — DocumentReference write-back
- get_encounter() — Encounter resource
- submit_prior_auth() — FHIR Claim/$submit (Da Vinci PAS)

What gets overridden in subclasses (EHR-specific only):
- Athena: submit_prior_auth() → CoverMyMeds API (Athena doesn't support FHIR PAS)
- Epic: get_patient_context() → optional proprietary extension enrichment
- Cerner: get_patient_context() → coverage fallback if payer field incomplete

Rule: if code only works on one EHR, it belongs in a subclass. If it works
on all EHRs using standard FHIR, it belongs in base.py.

## GitHub Actions & Templates

### Branch Rules (configure in GitHub repo Settings → Branches)
- `main` is protected: no direct commits, PRs required, CI must pass before merge
- `feature/*` — day to day work branches
- `release/*` — triggers production deploy (Phase 6+)
- Commit message format: `type(scope): description [TASK-XXX]`
  - types: feat, fix, test, refactor, chore, docs
  - scope: service or package name (track-b-rag, hipaa-logger, web, etc.)
  - example: `feat(track-b-rag): implement policy query endpoint [TASK-012]`

### .github/CODEOWNERS
```
# Everything — Mohamed owns all of it for now
*   @mohamedbouchtout

# Infra and compliance require extra attention
/infrastructure/terraform/environments/production/   @mohamedbouchtout
/docs/compliance/                                    @mohamedbouchtout
/packages/hipaa-logger/                              @mohamedbouchtout
```

### .github/workflows/ci.yml
Triggers on: pull_request to main, push to main
Jobs:
1. `detect-changes` — uses dorny/paths-filter to find which services/packages changed
2. Per-service test jobs (only run if that service changed):
   - `ruff check` + `ruff format --check` (linting)
   - `mypy src/` (type checking)
   - `pytest tests/ --cov=src --cov-fail-under=80`
3. `security-scan` — runs bandit -r . -ll on changed Python services
4. All jobs must pass before PR can merge

Path filter groups (each maps to a test job):
- `hipaa-logger`: packages/hipaa-logger/**  (own job — was previously only
  triggering service jobs via the packages/** wildcard below, and never
  actually ran its own tests. Fixed: packages get dedicated jobs too.)
- `crypto-utils`: packages/crypto-utils/**
- `fhir-types`: packages/fhir-types/** — this job runs BOTH checks: pytest against
  the Pydantic models AND `tsc --noEmit` against packages/fhir-types/typescript/.
  It is the only package with two languages in one job. The TypeScript side is
  its own npm workspace (see TASK-004) so tsc actually catches drift between
  the Pydantic models and their TS mirrors, not just compiles them in isolation.
- `track-b-rag`: services/track-b-rag/** or packages/**
- `track-a-clinical`: services/track-a-clinical/** or packages/**
- `audio-ingestion`: services/audio-ingestion/** or packages/**
- `fhir-integration`: services/fhir-integration/** or packages/**
- `nudge-service`: services/nudge-service/** or packages/**
- `prior-auth`: services/prior-auth/** or packages/**
- `policy-scraper`: services/policy-scraper/** or packages/**
- `web`: apps/web/**
- `mobile`: apps/mobile/**

Rule: any directory under packages/ needs its own path-filter entry AND its own
test job — a change under packages/ correctly re-runs every service that depends
on it, but that is not a substitute for running the package's own test suite.
The 80% coverage gate applies to packages/ the same as services/.

### .github/workflows/deploy-dev.yml
Stub file only during Phases 0-5. Content:
```yaml
# Deploy to dev — enabled in Phase 6 when infrastructure is ready
# on:
#   push:
#     branches: [main]
name: Deploy Dev (stub)
on: workflow_dispatch  # manual trigger only for now
jobs:
  placeholder:
    runs-on: ubuntu-latest
    steps:
      - run: echo "Deploy pipeline not yet configured"
```

### .github/PULL_REQUEST_TEMPLATE.md
PR template enforces task linkage and HIPAA checklist on every merge:
```markdown
## Task
Closes TASK-XXX

## What changed
<!-- One paragraph description -->

## Test evidence
<!-- Paste pytest output or screenshot -->

## HIPAA checklist
- [ ] No PHI appears in logs, print statements, or error messages
- [ ] Any new PHI access calls hipaa-logger audit_log()
- [ ] No secrets or credentials added to code or comments
- [ ] Audio data not written to disk anywhere in this change
- [ ] New environment variables added to .env.example

## For reviewer
<!-- Anything specific to look at or known tradeoffs -->
```

### .github/ISSUE_TEMPLATE/task.md
```markdown
---
name: Task
about: Implement a TASKS.md item
---
**Task ID:** TASK-XXX
**Phase:** 0 / 1 / 2 / 3 / 4 / 5 / 6 / 7

## What to build
<!-- Copy from TASKS.md -->

## Acceptance criteria
<!-- Copy test bullets from TASKS.md -->

## Notes
<!-- Decisions made, context for implementer -->
```

### .github/dependabot.yml
Note: this file goes in .github/ directly, NOT in .github/workflows/.
GitHub reads it natively — it is not a GitHub Actions workflow file.

```yaml
version: 2
updates:

  # Python — one entry per service and package (uv not yet natively supported,
  # use pip ecosystem which reads pyproject.toml)
  - package-ecosystem: "pip"
    directory: "/packages/hipaa-logger"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "python"]
    open-pull-requests-limit: 3

  - package-ecosystem: "pip"
    directory: "/packages/crypto-utils"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "python"]
    open-pull-requests-limit: 3

  - package-ecosystem: "pip"
    directory: "/services/audio-ingestion"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "python"]
    open-pull-requests-limit: 3

  - package-ecosystem: "pip"
    directory: "/services/track-a-clinical"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "python"]
    open-pull-requests-limit: 3

  - package-ecosystem: "pip"
    directory: "/services/track-b-rag"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "python"]
    open-pull-requests-limit: 3

  - package-ecosystem: "pip"
    directory: "/services/fhir-integration"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "python"]
    open-pull-requests-limit: 3

  - package-ecosystem: "pip"
    directory: "/services/nudge-service"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "python"]
    open-pull-requests-limit: 3

  - package-ecosystem: "pip"
    directory: "/services/prior-auth"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "python"]
    open-pull-requests-limit: 3

  - package-ecosystem: "pip"
    directory: "/services/policy-scraper"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "python"]
    open-pull-requests-limit: 3

  # npm — covers apps/web and apps/mobile (reads root package.json workspaces)
  - package-ecosystem: "npm"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "javascript"]
    open-pull-requests-limit: 5
    ignore:
      # Expo SDK updates are intentional — do manually when ready
      - dependency-name: "expo"
        update-types: ["version-update:semver-major"]
      - dependency-name: "expo-*"
        update-types: ["version-update:semver-major"]

  # GitHub Actions — keeps action versions current (security important)
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "github-actions"]
    open-pull-requests-limit: 3

  # Docker base images
  - package-ecosystem: "docker"
    directory: "/services/audio-ingestion"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "docker"]

  - package-ecosystem: "docker"
    directory: "/services/track-b-rag"
    schedule:
      interval: "weekly"
      day: "monday"
    labels: ["dependencies", "docker"]

  # Add remaining services as Dockerfiles are created
```

Dependabot behavior in this project:
- Opens PRs automatically on Monday for any outdated dependency
- CI runs on each Dependabot PR — if tests pass, you can merge with one click
- `open-pull-requests-limit: 3` per ecosystem prevents getting flooded with 20 PRs at once
- Expo SDK major versions are ignored because those require intentional migration work,
  not automatic bumps
- GitHub Actions versions are monitored separately — a compromised action in ci.yml
  is a supply chain attack vector, keeping them pinned and current matters

When a Dependabot PR opens:
1. Check if CI passes
2. Glance at the changelog link it includes
3. Merge if green — do not let these pile up
Security patches (CVE fixes) should be merged same day regardless of other work.
```markdown
---
name: Bug report
about: Something isn't working
---
**Service affected:** 
**TASK where this was introduced:** TASK-XXX

## What happened

## What was expected

## Steps to reproduce

## Logs
<!-- No PHI in logs — redact before pasting -->
```

### packages/crypto-utils — Design Decisions (locked, do not revisit)
**Scope note:** field-level AES-256-GCM encryption using a KMS-wrapped DEK per
record. This is for encrypting specific sensitive fields before they hit the
database — it is not a replacement for encryption-at-rest (RDS/S3 handle that
separately) and it is not a general crypto toolkit.
- **Never log plaintext.** Plaintext DEKs and plaintext field values must never
  reach any log line, exception message, or stack trace. If an encrypt/decrypt
  call fails, the exception message names the field/context being processed —
  never the plaintext value or the unwrapped key material. This applies inside
  the crypto primitives themselves, not just at call sites that happen to touch PHI.
- **Encryption context is bound in two places, not one.** The `context: dict`
  passed to `encrypt_field()` is used both as the KMS encryption context (on the
  DEK wrap/unwrap call) and as AES-GCM's AAD (additional authenticated data) on
  the local encrypt/decrypt operation. Binding only at the KMS layer would let
  ciphertext for one record's field be swapped onto another record and still
  decrypt successfully, since GCM alone has no knowledge the ciphertext was
  scoped to a specific record. Binding the same context as AAD makes GCM's
  authentication tag itself reject a mismatched context — defense in depth,
  independent of whether the KMS-side check is ever bypassed.
- **Moto (`@mock_aws`) for all KMS mocking in tests** — not hand-rolled
  `unittest.mock` on boto3 calls. Applies to every test that touches KMS.

`encrypt_field()` / `decrypt_field()` signatures:
```python
def encrypt_field(plaintext: str, context: dict[str, str]) -> EncryptedField:
    """context is bound as both KMS encryption context and GCM AAD."""
    ...


def decrypt_field(encrypted: EncryptedField, context: dict[str, str]) -> str:
    """Raises if context does not match what the field was encrypted with —
    this is the intended behavior, not an edge case to work around."""
    ...
```

## Current Implementation Status
See TASKS.md for what is built, what is in progress, and what is next.
Always check TASKS.md before starting new work to avoid duplicating effort.
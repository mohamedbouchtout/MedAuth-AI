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
```

## Key Architectural Constraints
- **Audio never persists.** Process in-memory BytesIO buffers only. Discard immediately after transcription.
- **Claude is called via AWS Bedrock only** (not Anthropic's direct API). This is the HIPAA-eligible path.
- **No Kafka until >20 providers.** Redis pub/sub for now. The service interfaces are identical so swapping later is a config change.
- **Qdrant for vector store.** Do not use Pinecone or Weaviate — we self-host for PHI control even though insurance policy text is not PHI (defense in depth).
- **Cache RAG results in Redis** with 24h TTL keyed by `rag:{payer}:{plan_type}:{state}:{cpt_code}`. This is a major cost lever — implement it from the start.
- **Haiku for extraction tasks, Sonnet for reasoning.** ICD/CPT entity extraction → Haiku. SOAP generation and payer policy analysis → Sonnet. Costs 15x less per extraction call.

### packages/hipaa-logger — Design Decisions (locked, do not revisit)
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
- **Defensive DSN normalization.** `normalize_dsn()` strips a SQLAlchemy driver suffix
  (`postgresql+asyncpg://` → `postgresql://`) so a single `DATABASE_URL` value serves
  this package and the SQLAlchemy services alike; `sqlalchemy_dsn()` adds it back for
  Alembic.
- **`version_table = "alembic_version_hipaa_logger"`.** Several Alembic setups share one
  database; on the default `alembic_version` table each would read another's revision as
  its own head. Every setup namespaces its own as `alembic_version_{name}` — this applies
  to every future migration setup, not just this package and track-a-clinical.

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
    ip_address: str | None = None,  # trailing optionals — otherwise these columns
    user_agent: str | None = None,  # would always be NULL
    conn: asyncpg.Connection | None = None,  # injection hook — uses pool if omitted
) -> None: ...
```


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
- `hipaa-logger`: packages/hipaa-logger/** or packages/crypto-utils/**
- `track-b-rag`: services/track-b-rag/** or packages/**
- `track-a-clinical`: services/track-a-clinical/** or packages/**
- `audio-ingestion`: services/audio-ingestion/** or packages/**
- `fhir-integration`: services/fhir-integration/** or packages/**
- `nudge-service`: services/nudge-service/** or packages/**
- `web`: apps/web/**
- `mobile`: apps/mobile/**

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

## Current Implementation Status
See TASKS.md for what is built, what is in progress, and what is next.
Always check TASKS.md before starting new work to avoid duplicating effort.

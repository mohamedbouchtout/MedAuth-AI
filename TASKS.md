# MedAuth AI — Task Breakdown

This file tracks implementation status. Update it as work completes.
Claude Code should read this before starting any task to understand current state.

## Status Legend
- [ ] Not started
- [~] In progress
- [x] Complete

---

## Phase 0 — Repository Scaffolding

- [x] **TASK-001:** Initialize monorepo structure
  - Create all directories per CLAUDE.md structure
  - Root `pyproject.toml` with uv workspace config covering all services/ and packages/
    (fhir-integration is Python — do not create a Node.js workspace for it)
  - Root `package.json` with npm workspaces covering apps/web and apps/mobile
    (a third workspace, packages/fhir-types/typescript/, is added later in
    TASK-004 once that package exists — don't scaffold it here)
  - `.gitignore` (Python, Node, Terraform, .env files)
  - `.env.example` with all required variable names, no values
  - `docker-compose.yml` (postgres, redis, qdrant, hapi-fhir)
  - `scripts/seed-synthea.sh` and `scripts/setup-dev.sh` — create as empty stubs
    with a comment: "# Implemented in TASK-052". Do not implement them now.
  - `.github/` directory with the following (details below in GitHub scaffold spec):
    - `dependabot.yml` — automated dependency updates (goes in .github/, NOT workflows/)
    - `workflows/ci.yml` — runs on every PR: lint, type-check, pytest per changed service
    - `workflows/deploy-dev.yml` — stub only, no real deploy yet (uncomment in Phase 6)
    - `PULL_REQUEST_TEMPLATE.md` — PR checklist
    - `ISSUE_TEMPLATE/bug_report.md` — bug report template
    - `ISSUE_TEMPLATE/task.md` — task implementation template
    - `CODEOWNERS` — you own everything for now
  - **Test:** open a draft PR against main, verify ci.yml triggers and runs

- [x] **TASK-002:** Create shared `packages/hipaa-logger`
  - Owns its own audit_log table — see CLAUDE.md "hipaa-logger — Design Decisions"
    for the full schema and the reasoning for this ownership split
  - Alembic migration in `packages/hipaa-logger/migrations/` creating the audit_log table
  - Set `version_table="alembic_version_hipaa_logger"` in this migration's env.py —
    see CLAUDE.md "Alembic version table isolation." Every future Alembic setup in
    this repo follows the same `alembic_version_{name}` pattern.
  - Raw asyncpg (not SQLAlchemy) — self-managed lazy connection pool from DATABASE_URL,
    with an injection hook accepting an optional `conn` param for tests and for callers
    that want the audit write inside their own transaction
  - Strip the `+asyncpg` driver suffix from DATABASE_URL defensively on connect —
    see CLAUDE.md "DATABASE_URL format" for the one-liner. Don't require a second env var.
  - `async def audit_log(actor_id, action, resource_type, resource_id, session_id, service_name, request_id=None, ip_address=None, user_agent=None, conn=None)`
    — exact signature is in CLAUDE.md, keep them in sync if either changes
  - Unit tests with mocked asyncpg (mock the pool/connection, not a real DB)
  - Integration test against real local Postgres: run the migration, call audit_log(),
    verify the row lands with correct columns
  - Update `.github/workflows/ci.yml` (created in TASK-001) to add a dedicated
    `hipaa-logger` path-filter entry and test job — the original ci.yml only
    triggered service jobs on packages/** changes and never ran the package's
    own tests. See CLAUDE.md CI section for the corrected path filter list.
    Verify: open a PR touching only packages/hipaa-logger/**, confirm the
    hipaa-logger job runs and no service jobs run unnecessarily.

- [x] **TASK-003:** Create shared `packages/crypto-utils`
  - AES-256-GCM encrypt/decrypt using a KEK from AWS KMS + per-record DEK
  - `encrypt_field(plaintext: str, context: dict) -> EncryptedField`
  - `decrypt_field(encrypted: EncryptedField, context: dict) -> str`
  - Bind `context` in both places: as KMS encryption context on the DEK wrap/unwrap
    AND as AES-GCM AAD on the local encrypt/decrypt — see CLAUDE.md "crypto-utils —
    Design Decisions" for why. A context mismatch on decrypt must raise, not silently
    proceed with the wrong key or the wrong field binding.
  - HIPAA constraint: plaintext DEKs and plaintext field values must never appear in
    any log line, exception message, or stack trace — including inside the crypto
    primitives themselves, not just at PHI-touching call sites.
  - Moto (`@mock_aws`) for all KMS mocking — not hand-rolled unittest.mock on boto3
  - Unit tests with mocked KMS
  - Verify CI collects these tests under a dedicated `crypto-utils` job, not folded
    into `hipaa-logger`'s job — confirm by checking the path-filter output on a PR
    that touches only packages/crypto-utils/**
  - Verified on PR #18 (54 tests, 91% coverage): the run produced
    `Test (packages/crypto-utils)` and `Type-check (mypy) (packages/crypto-utils)`
    as their own jobs, and no hipaa-logger job at all. ci.yml needed no change —
    TASK-002 already replaced the old path-filter groups with full workspace-member
    paths, so every packages/ directory gets its own job automatically.

- [x] **TASK-004:** Create shared `packages/fhir-types`
  - Pydantic v2 models for FHIR R4 resources used by this project
  - Patient, Encounter, Condition, Coverage, MedicationRequest, DocumentReference, Claim
    — seven resources, matching CLAUDE.md's Tech Stack list. (An earlier six-item
    draft of this task dropped MedicationRequest — that was an omission, not an
    intentional exclusion. The prior-auth bundle needs it for medication-based
    authorizations like biologics and chemotherapy.)
  - Mirror in TypeScript interfaces under `packages/fhir-types/typescript/`, consumed
    by `apps/web` and `apps/mobile` (both use fhirclient.js for the SMART launch).
    fhir-integration is a Python service like every other backend service — the
    TypeScript mirrors are not for it. (An earlier draft said "for the fhir-integration
    Node service" — stale, from before fhir-integration's language was settled.)
  - Add `packages/fhir-types/typescript/` as a third npm workspace, with its own
    `package.json` and `tsconfig.json` — not plain .ts files consumed via a path alias.
    The workspace approach means CI actually type-checks the mirrors against drift
    from the Pydantic models; plain files would leave them unverified, which defeats
    the purpose of having mirrors at all.
  - Add a `fhir-types` job to `.github/workflows/ci.yml`'s path-filter matrix
    (`packages/fhir-types/**` → dedicated job, `tsc --noEmit` at minimum). Same
    category of gap as the packages/ CI matrix fix from TASK-002 — don't repeat it.
  - Built (85 tests, 100% coverage). Decisions worth knowing before touching this
    package:
    - Field names are snake_case in Python and camelCase on the wire, via a Pydantic
      camel alias generator. **Always dump with `by_alias=True, exclude_none=True`** —
      a server rejects snake_case element names, and FHIR has no null elements.
    - `extra="allow"`, so elements this package does not model survive a round trip
      instead of being dropped. A resource written back to an EHR keeps what it
      arrived with. The cost is that a misspelled field is accepted as an extra.
    - FHIR `date`/`dateTime` are `str`, not `datetime.date`. Reduced precision
      (`"1975"`, `"1975-03"`) is legal in FHIR and real EHRs send it; parsing into
      a `date` would reject valid data or invent a day.
    - Only *required*-binding value sets are `Literal` (in `codes.py`). Extensible
      bindings stay `CodeableConcept` — constraining those would reject valid payloads.
    - `Encounter.class` and `Coverage.class` are `encounter_class` / `coverage_class`
      in Python with an explicit `alias="class"`, since `class` is a keyword.
    - Drift between the two languages is a test, not a convention:
      `tests/unit/test_typescript_parity.py` compares every model's element names
      against its TypeScript interface (resolving `extends`) and every `Literal`
      against its string-literal union in `codes.ts`. It parses the mirrors with a
      regex, so `typescript/src/` must keep one property per line and no inline
      object literal types; a file that stops parsing fails rather than passing
      silently.
    - The CI job runs pytest and `tsc --noEmit` together, so its verdict means the
      two representations agree. Its pytest run overlaps the `test` matrix entry for
      this package on purpose — the matrix keeps packages under uniform rules, the
      dedicated job stays meaningful on its own.

- [x] **TASK-005:** Initialize database schema + migrations
  - Create Alembic setup in `services/track-a-clinical` (owns migration authorship —
    see CLAUDE.md "Migration Ownership vs. Table Write Access" for what this does
    and doesn't mean; other services read/write these tables via the same
    DATABASE_URL without owning their migrations)
  - Set `version_table="alembic_version_track_a_clinical"` per the isolation
    pattern established in TASK-002 — every Alembic setup in this repo gets its
    own version table, no exceptions
  - Inline schema (this is the authoritative source — no external doc to consult):
    ```sql
    CREATE TABLE encounters (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id UUID UNIQUE NOT NULL,
        ehr_encounter_id VARCHAR(100),
        patient_fhir_id VARCHAR(100) NOT NULL,
        provider_id UUID NOT NULL,
        organization_id UUID,
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ended_at TIMESTAMPTZ,
        insurance_payer VARCHAR(200),
        insurance_plan_type VARCHAR(100),
        insurance_member_id VARCHAR(100),
        deleted_at TIMESTAMPTZ  -- soft delete, see Code Conventions
    );

    CREATE TABLE clinical_notes (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        encounter_id UUID NOT NULL REFERENCES encounters(id),
        soap_subjective TEXT,
        soap_objective TEXT,
        soap_assessment TEXT,
        soap_plan TEXT,
        icd10_codes JSONB,
        cpt_codes JSONB,
        ehr_document_ref_id VARCHAR(100),
        generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        reviewed_by_provider BOOLEAN NOT NULL DEFAULT FALSE,
        provider_edited BOOLEAN NOT NULL DEFAULT FALSE,
        deleted_at TIMESTAMPTZ
    );

    CREATE TABLE clinical_nudges (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        encounter_id UUID NOT NULL REFERENCES encounters(id),
        procedure_name VARCHAR(200),
        cpt_code VARCHAR(20),
        nudge_message TEXT,
        missing_criteria JSONB,
        denial_risk VARCHAR(20),
        payer_policy_source VARCHAR(500),
        fired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
        acknowledged_at TIMESTAMPTZ,
        resulted_in_documentation BOOLEAN
    );

    CREATE TABLE prior_auth_requests (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        encounter_id UUID NOT NULL REFERENCES encounters(id),
        status VARCHAR(50) NOT NULL DEFAULT 'pending',
        payer_name VARCHAR(200),
        procedures JSONB,
        diagnoses JSONB,
        clinical_evidence JSONB,
        submission_method VARCHAR(50),
        payer_reference_number VARCHAR(200),
        submitted_at TIMESTAMPTZ,
        decided_at TIMESTAMPTZ,
        denial_reason TEXT
    );

    CREATE TABLE insurance_policies (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        payer VARCHAR(200) NOT NULL,
        plan_type VARCHAR(100),
        state CHAR(2),
        policy_id VARCHAR(200) UNIQUE NOT NULL,
        source_url TEXT,
        content_hash VARCHAR(64) NOT NULL,  -- SHA-256 hex digest, see TASK-011
        last_ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        effective_date DATE,
        qdrant_collection VARCHAR(100) NOT NULL DEFAULT 'insurance_policies'
    );

    CREATE INDEX idx_encounters_session ON encounters(session_id);
    CREATE INDEX idx_encounters_provider ON encounters(provider_id);
    CREATE INDEX idx_clinical_notes_encounter ON clinical_notes(encounter_id);
    CREATE INDEX idx_clinical_nudges_encounter ON clinical_nudges(encounter_id);
    CREATE INDEX idx_prior_auth_encounter ON prior_auth_requests(encounter_id);
    CREATE INDEX idx_prior_auth_status ON prior_auth_requests(status);
    CREATE INDEX idx_insurance_policies_payer_state ON insurance_policies(payer, state);
    ```
  - audit_log is NOT created here — owned by packages/hipaa-logger (TASK-002),
    applied first. `scripts/init-db.sh` runs migrations in order: hipaa-logger
    first, then this migration set.
  - Seed script (`scripts/seed-test-encounters.py`) with 5 test encounters linked
    to Synthea patient IDs (requires TASK-052 / local HAPI FHIR to have patients
    loaded first — if run before then, seed with placeholder patient_fhir_id
    values and note that in a code comment)
  - **Test:** `alembic upgrade head` creates all five tables and all seven indexes
  - **Test:** `alembic downgrade base` drops all five and leaves audit_log and
    hipaa-logger's version table standing — the histories are independent in both
    directions, not just on the way up
  - **Test:** the namespaced version table exists and `alembic_version` does not
  - **Test:** `compare_metadata` against the migrated database returns no
    differences, so the models and the migration cannot drift apart silently
  - **Test:** seed script smoke — inserts 5 encounters, a second run inserts none,
    and a missing DATABASE_URL exits non-zero with a message rather than a traceback
  - Built (28 tests, 100% coverage). Decisions worth knowing before touching this:
    - The mapped classes live in `src/track_a_clinical/models/`, one module per
      table, and are the single definition of these tables for the whole monorepo
      — see CLAUDE.md "Where the shared SQLAlchemy models live." The service builds
      a named package rather than a bare `src/` precisely so other services can
      import them; every other service still ships a top-level `src` and they
      shadow one another in the shared venv.
    - `env.py` sets `target_metadata = Base.metadata` rather than `None` (which is
      what hipaa-logger does, since it writes SQL by hand). That is what makes the
      autogenerate-diff test possible, and it is the only mechanism that catches a
      model edited without a migration.
    - `include_object` excludes `audit_log` and `alembic_version_hipaa_logger`.
      Without it, autogenerate in this service would propose dropping tables that
      belong to hipaa-logger's history simply because they are absent from these
      models.
    - `clinical_nudges` and `prior_auth_requests` have no `deleted_at`, matching
      the schema above. They are records of what the system did — a nudge shown to
      a provider, a bundle sent to a payer — so nothing retires them, the same
      reasoning that keeps audit_log append-only. `encounters` and `clinical_notes`
      do get the soft-delete column.
    - The seed script derives every id with `uuid5` from a fixed namespace, so
      re-running it is a no-op instead of a duplicate. `patient_fhir_id` values are
      `synthea-placeholder-N` until TASK-052 loads HAPI FHIR; the module docstring
      says what to swap them for.
    - CI needed no new job. The existing `test` matrix now runs
      `scripts/init-db.sh` after `uv sync` and before pytest, so every member finds
      the schema already applied — `track-a-clinical`'s own integration tests
      re-run `upgrade head` anyway, which is a no-op on a current database.

- [x] **TASK-006:** Session lifecycle endpoints
  - Service: `services/track-a-clinical` (owns the encounters table)
  - Full spec is in CLAUDE.md "Session Lifecycle & JWT Issuance" — read that first,
    this task did not exist in earlier drafts and several later tasks assume it:
    TASK-020, TASK-021, TASK-030, TASK-041, and TASK-060 all depend on this.
  - **This is the first HTTP surface in the repo.** track-a-clinical currently
    ships only models, db.py, and migrations (TASK-005) — no FastAPI app exists
    yet anywhere in the monorepo. This task also creates:
    - `services/track-a-clinical/src/main.py` — FastAPI app entrypoint, port 8003
      per the Local Development port table
    - `docs/api/track-a-clinical.yaml` — OpenAPI spec (docs/api/ is currently empty)
  - Add `pyjwt` to `services/track-a-clinical/pyproject.toml` — not yet a dependency
  - Import `Encounter` from `track_a_clinical.models` (TASK-005) — do not define
    a second model class against the same table
  - `POST /sessions/start` — body: `{patient_id, provider_id, ehr_encounter_id}`
    (Pydantic request model). `patient_id` on the wire maps to the model's
    `patient_fhir_id` column — names differ deliberately, see CLAUDE.md.
    Creates `encounters` row (`session_id` generated server-side as a UUID,
    never client-supplied). Mints JWT: claims `{session_id, provider_id, exp}`
    only — no `iss`/`aud` for v1 even though those env vars exist unused in
    .env.example (see CLAUDE.md for why). Lifetime from `SESSION_TTL_SECONDS`
    (default 900), not a hardcoded literal. Signed with `JWT_SIGNING_KEY`.
    Response: `{"data": {"session_id": ..., "jwt": ...}, "error": null}` per
    the standard API envelope. All PHI access logged via hipaa-logger.
  - `POST /sessions/{session_id}/end` — sets `status='completed'`, `ended_at`,
    publishes `session:ended:{session_id}` to Redis (empty payload, signal only).
    404 if `session_id` unknown or belongs to a soft-deleted encounter. Idempotent:
    ending an already-completed session returns 200 without publishing a second
    Redis signal (downstream consumers would otherwise double-fire).
  - `JWT_SIGNING_KEY` already exists in `.env.example` — confirm it's there
    rather than re-adding it. `JWT_ISSUER`, `JWT_AUDIENCE`, `SESSION_TTL_SECONDS`
    also already exist from an earlier scaffold pass — only `SESSION_TTL_SECONDS`
    is used by this task; leave the other two unused/undocumented for now per
    the v1 claim-set decision above.
  - Both routes: Pydantic request/response models, hipaa-logger call, OpenAPI
    docstring, at least one integration test — per the per-route requirements
    in Known Constraints.
  - **Test:** start a session, verify JWT decodes with correct claims (session_id,
    provider_id, exp) and expiry matches SESSION_TTL_SECONDS
  - **Test:** end a session, verify Redis subscriber receives the signal
  - **Test:** end an unknown session_id, verify 404
  - **Test:** end an already-completed session twice, verify second call
    returns 200 and does not publish a second Redis signal
  - Built (76 tests, 100% coverage). Decisions worth knowing before touching this:
    - `main.py` lives at `src/track_a_clinical/main.py`, not `src/main.py`. This
      service ships a named package so other services can import its models, and
      a top-level `src` module here would shadow the one every other service
      installs into the shared venv. Run it as
      `uv run uvicorn track_a_clinical.main:app --port 8003`.
    - The audit write joins the request's own transaction rather than running on
      hipaa-logger's pool: `db.raw_asyncpg_connection()` hands the session's
      asyncpg connection to `audit_log(conn=...)`. An encounter therefore cannot
      exist without its audit row, and neither can survive the other's failure.
    - `POST /sessions/start` returns **201**, not 200 — it creates a resource.
      TASK-070's "start visit" button should expect 201.
    - `JWT_SIGNING_KEY` must be at least 32 bytes; `Settings` rejects anything
      shorter. HS256 with a secret below the digest length is a weak MAC, and
      PyJWT warns about it. Set a real value in `.env.local` before running.
    - Idempotent end returns the *original* `ended_at`, not a new one, and audits
      as `READ_ENCOUNTER` rather than `END_SESSION` — the row was read, not
      changed. `already_ended` in the response tells a client which happened.
    - Known gap, deliberately not solved here: the Redis publish follows the
      commit, so a broker failure ends the encounter without signalling it, and
      the idempotent retry will not re-send. The endpoint answers 503 so the
      failure is visible instead of silently stranding TASK-030 and TASK-060.
      A durable outbox is the real fix and belongs with the consumers.
    - `docs/api/track-a-clinical.yaml` is hand-maintained but not unchecked:
      `tests/unit/api/test_openapi_contract.py` compares it to the app's
      generated schema on routes, methods, status codes and required request
      fields, so the spec cannot drift silently. Descriptions are not compared.
    - CI needed no change. `pyyaml` moved into the root dev group for that drift
      test rather than being relied on transitively through moto.

---

## Phase 1 — RAG Pipeline (Build This First)

The insurance policy RAG is the technical core. Build and validate before other services.

- [x] **TASK-010:** Set up Qdrant collection + embedding model
  - Service: `services/track-b-rag`
  - **Package rename, do this first:** rename to `src/track_b_rag/` (not bare
    `src/`) and declare `medauth-track-a-clinical` as a dependency in
    `pyproject.toml`. This service is empty right now, so the rename is free —
    TASK-011 imports the shared SQLAlchemy models from `track_a_clinical.models`
    to write the `insurance_policies` row, which means track-b-rag crosses the
    service-import boundary one task from now. See CLAUDE.md's package-naming
    note for the general rule this follows.
  - Initialize Qdrant `insurance_policies` collection (cosine, 1024 dims) using the
    idempotent get-or-create pattern in CLAUDE.md "Qdrant Initialization — Must Be
    Idempotent." Do NOT use `recreate_collection()` in startup code — it deletes
    all indexed policies on every service restart, which is a real bug carried
    over from an earlier draft of this architecture and must not ship.
  - Load `BAAI/bge-large-en-v1.5` embedding model via sentence-transformers,
    behind a lazy-loaded singleton — don't load it at import time. Mock it in
    unit tests; the one test that needs the real (1024,)-shape assertion is an
    integration test, not a unit test. Add HuggingFace's `~/.cache/huggingface`
    to the CI workflow's cache config so the ~1.3GB download isn't repeated on
    every run.
  - `GET /health` returns the standard envelope, not a bare object:
    `{"data": {"qdrant": "ok"|"error", "embedding_model": "ok"|"error"}, "error": null}`
    — 200 only if both are ok, 503 otherwise. track-a-clinical's existing
    `api/envelope.py` is the pattern to reuse.
  - `/health` is exempt from the "every route calls hipaa-logger" constraint —
    see CLAUDE.md's hipaa-logger scope note for why this is a standing
    exception, not a one-off judgment call for this task.
  - **Test:** embed a query string (mocked model), verify the call path works
  - **Test (integration):** embed a query string with the real model, verify
    vector shape is (1024,)
  - **Test (integration, requires live Qdrant):** call ensure_collection() twice
    in a row against a populated collection, verify no data loss — this cannot
    be proven against a mock, it needs the real container (available in CI and
    docker-compose). This is the regression test for the recreate_collection bug.
  - Built (80 tests, 100% coverage: 63 in track-b-rag, 17 in the new
    `packages/api-envelope`). Decisions worth knowing before touching this
    service:
    - Run it as `uv run uvicorn track_b_rag.main:app --port 8002` — the rename
      landed, so `src.main:app` is gone. `medauth-track-a-clinical` is declared
      as a dependency but not yet imported; TASK-011 is what uses it.
    - The recreate_collection guard is tested three ways, not one: the unit
      fake has no `recreate_collection` attribute at all (a call would raise
      AttributeError rather than silently pass), a source-level assertion
      rejects any `.recreate_collection(` call site, and the integration test
      upserts points and re-runs `ensure_collection` five times against a real
      container. Verified by reintroducing the bug on purpose — all three fail.
    - `ensure_collection()` returns True/False for created/already-existed, and
      treats a concurrent create by another replica as success. Several pods
      booting together all run this; losing that race still means the
      collection exists, which is what was asked for.
    - **Startup does not fail on an unreachable Qdrant.** It logs and continues,
      and `/health` answers 503 until the container is up. A crash loop on a
      slow dependency hides the cause; a 503 naming `qdrant` shows it.
    - The embedding model is never loaded at import or at startup — only on
      first use, which in practice is the first health probe. Loading 1.3GB of
      weights before the port opens makes every rollout look like a failure to
      the orchestrator. `sentence_transformers` is imported *inside*
      `get_embedder()` for the same reason: at module scope it drags in torch.
    - **A 503 from `/health` still carries `data`, not `error`.** The request
      succeeded; the answer is "unhealthy". Putting the flags in the error half
      would discard the only diagnostic the endpoint has. Both checks run
      concurrently in a worker thread — they are blocking calls and one of them
      can take seconds.
    - `docs/api/track-b-rag.yaml` is hand-maintained and drift-tested the same
      way track-a-clinical's is (`tests/unit/api/test_openapi_contract.py`),
      comparing routes, methods, status codes and the health payload's fields.
    - The response envelope is **not** in this service. It was extracted to
      `packages/api-envelope` in this task, once track-b-rag became its second
      consumer: `from api_envelope import ApiResponse, install_error_handlers`.
      track-a-clinical was migrated onto it in the same change, so neither
      service defines its own. `error_responses()` gained a `descriptions=`
      override so track-a's route-specific 404/503 wording survived the move.
      Any service added later imports from there — do not copy it again.
    - CI needed two changes, both in the `test` job: an `actions/cache` step for
      `~/.cache/huggingface` scoped to this member, and `RUN_EMBEDDING_TESTS=1`.
      The embedding integration tests are opt-in by env var so a developer
      running the suite locally does not trigger a 1.3GB download unasked. The
      Qdrant service container and `QDRANT_HOST`/`QDRANT_PORT` were already
      there from TASK-001 — this is the first task to use them.

- [x] **TASK-011:** Policy ingestion pipeline
  - Service: `services/track-b-rag`
  - **First Postgres touch in this service** — no `db.py` exists here yet. Create
    one mirroring `track_a_clinical/db.py` (engine + async_sessionmaker reading
    `DATABASE_URL`) but WITHOUT the Alembic pieces: track-a-clinical owns
    migration authorship for `insurance_policies`, track-b-rag only writes rows.
    Import the `InsurancePolicy` model from `track_a_clinical.models` — do not
    define a second mapped class.
  - `POST /policies/ingest` accepts PDF file + metadata (payer, plan_type, state, policy_id)
    — internal service-to-service endpoint only (called by policy-scraper and by
    scripts/seed-policies.py), not exposed to any frontend app.
    **"Internal only" is currently network-level isolation only — no code
    enforcement.** The only auth in the repo is TASK-006's session JWT, which is
    scoped to a clinical encounter and makes no sense for a scraper CronJob.
    Do NOT invent a service-to-service auth scheme inside this task; document
    the assumption in the route docstring and leave it. A real internal-auth
    mechanism is its own future task.
  - No `audit_log()` call — insurance policies are public payer publications with
    no patient linkage, so this route touches no PHI (see corrected Known
    Constraints #6). Log the ingest at INFO via `logging.getLogger(__name__)`
    with policy_id, payer, and resulting status — the operational trace belongs
    there, not in the compliance table.
  - PDF parsing via PyMuPDF (fitz) — handles multi-column medical policy docs
  - Chunking: RecursiveCharacterTextSplitter, chunk_size=800, overlap=150
  - `content_hash` = SHA-256 hex digest of the raw PDF bytes (not the extracted
    text — two PDFs with identical text but different formatting should still be
    treated as distinct source files for audit purposes)
  - **Qdrant payload schema and indexes — decide here, not in TASK-012.** Payload
    fields per point: `policy_id`, `payer`, `plan_type`, `state`, `effective_date`,
    `chunk_index`, `text`. Create payload indexes on `policy_id` (needed by this
    task's delete-old-points path) and on `payer` + `state` (needed by TASK-012's
    query filter) — defining both now avoids reworking the collection a task later.
  - **Deterministic point IDs:** `uuid5(NAMESPACE, f"{policy_id}:{chunk_index}")`,
    the same trick `scripts/seed-test-encounters.py` already uses. Makes re-ingest
    naturally idempotent — a re-upsert overwrites in place rather than duplicating.
  - Dedup behavior: if `policy_id` already exists in `insurance_policies` with a
    matching `content_hash`, skip re-ingestion and return 200 with `{"status": "unchanged"}`.
    If `policy_id` exists with a different hash, re-ingest (delete old Qdrant points
    for that policy_id first, then insert new ones) and return `{"status": "updated"}`.
    If `policy_id` is new, ingest and return `{"status": "created"}`.
  - Embed chunks + upsert to Qdrant with metadata payload
  - Store metadata record in `insurance_policies` table
  - **Write Qdrant first, Postgres second.** The two stores are not in one
    transaction, so the ordering decides which way a partial failure fails. With
    Qdrant first, a crash between the two leaves the stored `content_hash` stale
    and the next scrape re-ingests — wasteful and self-correcting. Reversed, the
    row would claim to be current while the vectors are missing or half-written,
    and nothing would ever retry. Do not "optimize" this into a single
    write-the-row-first pass.
  - **Qdrant payload schema, fixed here** — every point carries `policy_id`,
    `payer`, `plan_type`, `state`, `chunk_index`, and the chunk `text`. Two
    payload indexes, both keyword: `policy_id` for this task's delete-by-filter
    path, and `payer` + `state` for TASK-012's retrieval filter. Create them with
    the same get-or-create shape as the collection itself — never unconditionally.
  - **Point IDs are deterministic:** `uuid5(namespace, f"{policy_id}:{chunk_index}")`
    against a fixed module-level namespace, the same trick
    `scripts/seed-test-encounters.py` uses. Re-ingesting the same document
    overwrites its own points rather than accumulating a second copy, so the
    dedup path is belt-and-braces instead of the only thing standing between a
    retry and duplicated chunks.
  - No `audit_log()` call — insurance policies are public payer publications with
    no patient linkage. See Known Constraints #6; log the ingest at INFO instead.
  - Access control is network isolation only, stated in the route docstring. No
    service-to-service auth is invented here; that is its own future task.
  - **Test:** ingest a sample 10-page PDF, verify chunks appear in Qdrant
  - **Test:** ingest the same PDF twice, verify second call returns "unchanged" and
    does not duplicate Qdrant points
  - **Test:** ingest a modified version of an existing policy_id, verify old points
    are removed and "updated" is returned
  - Built (179 tests, 100% coverage). Decisions worth knowing before touching this:
    - The route takes **one** body parameter — `Annotated[IngestPolicyRequest,
      File()]` — with the `UploadFile` as a *field of that model*. This is not a
      style choice: FastAPI only flattens a form model into its individual form
      fields when the model is the sole body parameter. Declaring `UploadFile`
      as a second parameter alongside it makes FastAPI look for a form field
      literally named `metadata`, and every well-formed request 422s. `File()`
      rather than `Form()` is what makes the published spec say
      `multipart/form-data` instead of `application/x-www-form-urlencoded`.
    - Optional form fields go through a `BeforeValidator` that maps blank to
      None. A multipart client renders an unset field as `name=""`, which would
      otherwise fail `min_length` or store an empty `plan_type`. `state` is
      uppercased the same way, for the `CHAR(2)` column and TASK-012's filter.
    - `source_url` and `effective_date` are accepted beyond TASK-011's four-field
      metadata list. Both columns exist on `insurance_policies` and TASK-013's
      scraper knows both values at call time — real optional parameters rather
      than columns permanently NULL, the same call CLAUDE.md makes for
      hipaa-logger's `ip_address`/`user_agent`.
    - The Postgres write is `INSERT ... ON CONFLICT DO UPDATE`, not
      read-then-branch. Two scrapers racing on the same new policy would both see
      no row and both insert, and the loser would surface an integrity error on
      an ordinary retry. The created/updated label comes from the read that
      already happened — the race can make the label optimistic, never the write
      wrong.
    - **An empty document is refused before anything is deleted.** A scanned PDF
      with no text layer parses cleanly and yields nothing; accepting it would
      write a `content_hash` saying "current" with zero vectors behind it, and
      every later ingest of those same bytes would then report `unchanged`. That
      is the one state the dedup logic cannot recover from on its own.
    - Imported as `pymupdf`, not `fitz`. Same library, but only the `pymupdf`
      module ships a `py.typed` marker, so this keeps the module inside mypy
      strict instead of needing an `ignore_missing_imports` override for the
      whole package. Its `Document` constructor is still unannotated, hence one
      targeted `type: ignore` at that single call site.
    - The integration tests stub the embedder with deterministic 1024-wide
      vectors rather than loading the real weights. What they test is the
      pipeline's effect on the two stores; requiring the real model would put
      them behind `RUN_EMBEDDING_TESTS` and leave TASK-011's three dedup claims
      unverified on most runs. TASK-010's suite covers the real model.
    - CI needed no change. The test job already starts postgres/redis/qdrant from
      docker-compose, applies both migration histories via `scripts/init-db.sh`,
      and sets `DATABASE_URL` and `QDRANT_HOST` — this is the first task in this
      service to use the database half.
    - Local-dev note: on Windows, `QDRANT_HOST=localhost` resolves to IPv6 first
      and hangs. Use `127.0.0.1`. CI is unaffected.

- [x] **TASK-012:** Policy query endpoint
  - Service: `services/track-b-rag`
  - **First Redis and Bedrock touch in this service.** `redis` and `langchain-aws`
    are declared in pyproject.toml but unused; Settings has only Qdrant and
    embedding fields. This task adds the Redis client module, the Bedrock client
    module, and their config fields. Redis joins `/health`'s dependency flags
    alongside `qdrant` and `embedding_model` — a route now hard-depends on it,
    so an unreachable Redis should surface as a named 503, not as latency.
  - `POST /policies/query` — Pydantic request model:
    `{procedure: str, cpt_code: str, payer: str, plan_type: str, state: str,
    clinical_context: dict, session_id: UUID, provider_id: UUID}`
    — `session_id` and `provider_id` are new relative to earlier drafts of this
    task: this route touches PHI (clinical_context carries encounter detail),
    so it needs an `audit_log()` call, and an audit row needs actor_id/session_id.
    TASK-021 is the caller and has both available.
  - Response model: `{requires_auth: bool, auth_criteria: list[str], missing_criteria: list[str],
    denial_risk: Literal["low","medium","high"], nudge_message: str, step_therapy_required: bool,
    step_therapy_details: str | None}` — wrapped in the standard envelope
  - **Calls `audit_log()`** — this route touches PHI, unlike `/policies/ingest`.
    Never log `clinical_context` contents; the audit row records the access
    (actor, session, resource type), not the clinical detail.
  - **Two-stage design — the cache holds payer-policy data only, never
    patient-specific data.** This is a correctness requirement, not an
    optimization detail. See CLAUDE.md's cache note in Key Architectural
    Constraints.
    - **Stage 1 (cached, 24h TTL, key `rag:{payer}:{plan_type}:{state}:{cpt_code}`):**
      resolve the payer's rules for this procedure — `requires_auth`,
      `auth_criteria`, `step_therapy_required`, `step_therapy_details`. On cache
      miss: embed query, search Qdrant (top 8, payer+state filter), call Claude
      **Sonnet** via `BEDROCK_MODEL_ID_REASONING` (multi-step reasoning over
      retrieved policy text — see CLAUDE.md's Bedrock Model Assignment table).
      Cache the result. These fields are identical for every patient with the
      same payer/plan/state/CPT.
    - **Stage 2 (never cached, runs every call):** compare the Stage 1
      `auth_criteria` against this request's `clinical_context` to produce
      `missing_criteria`, `denial_risk`, and `nudge_message`. These are
      properties of *this patient's documentation* — caching them across
      patients would serve patient B the gaps computed for patient A, which is
      a patient-safety bug, not a stale-cache annoyance.
    - Earlier drafts of this task cached the entire response under the Stage 1
      key. That was wrong and is corrected here. Do not "simplify" it back.
  - **Build Stage 1 behind a seam** — a single `resolve_policy_rules(...)`
    function that TASK-015 can put the Da Vinci CRD path in front of, falling
    through to this RAG implementation unchanged. Same response shape either
    way; callers never branch on which path answered. Cheaper to define the
    seam now than to refactor one task later.
  - Claude is instructed to return only JSON. If the response fails to parse as valid
    JSON matching the Stage 1 shape: retry once with the same prompt. If the retry
    also fails, return a safe fallback: `{requires_auth: true, auth_criteria: [],
    missing_criteria: [], denial_risk: "high", nudge_message: "Unable to verify
    authorization requirements — confirm manually", step_therapy_required: false,
    step_therapy_details: null}` and log the parse failure (no PHI in that log line —
    log the payer/procedure/cpt_code, never clinical_context contents). Fail toward
    "flag for manual review," never toward "assume no auth needed."
  - Cache the Stage 1 result only on a real (non-fallback) response — don't cache the fallback
  - Update `docs/api/track-b-rag.yaml` (drift test compares routes/methods/status codes)
  - **Test:** query for "knee MRI" + "Aetna PPO MA" — verify structured JSON returned
  - **Test:** second identical query — verify Stage 1 cache hit (no Bedrock call)
  - **Test (the correctness test for the two-stage split):** two queries with
    identical payer/plan/state/CPT but *different* `clinical_context` — verify
    Stage 1 is served from cache (no second Bedrock call for policy rules) AND
    that `missing_criteria` differs between the two responses. A single cached
    blob would fail this test; that's the point of it.
  - **Test:** mock Bedrock returning malformed JSON — verify retry, then fallback,
    verify fallback is not cached
  - **Test:** verify `audit_log()` is called with the request's session_id and
    provider_id, and that no clinical_context content appears in the audit row
  - Built (344 tests, 100% coverage). Decisions worth knowing before touching this:
    - The two stages live in separate modules and the split is enforced by a
      signature, not a comment: `policy_rules.resolve_policy_rules()` has no
      `clinical_context` parameter at all, so nothing patient-specific can reach
      the prompt, the retrieval or the cached value. A unit test asserts the
      parameter list, so adding one back fails the build rather than review.
    - `gap_analysis` (Stage 2) is **deterministic Python, not a second Bedrock
      call.** It has to be: TASK-012's cache-hit test says "no Bedrock call" on
      the second query, and a model call in Stage 2 would make that false. It
      also keeps the per-nudge latency inside what a live encounter tolerates
      and makes the output reproducible in a test. The matcher itself is an
      explicit term-overlap heuristic with a documented threshold, biased toward
      reporting a criterion missing — the replaceable part; the determinism and
      the bias are not.
    - Step therapy raises the denial-risk *floor* to medium rather than
      appearing in `missing_criteria`. A plan with a step therapy prerequisite
      is never a low-risk submission, but escalating every level would flag
      well-documented requests as high.
    - **The safe fallback covers more than a parse failure.** TASK-012 specifies
      it for an answer that will not parse; it is also returned for an
      unreachable Qdrant, a Bedrock error, and a retrieval that matched nothing.
      The endpoint therefore has no 5xx path. That is deliberate: TASK-021 fires
      nudges during a live encounter and reads an error as silence, and silence
      reads as "no authorization concern" — the one thing this service must
      never imply by accident. Outages surface through `/health` and an ERROR
      log instead. A fallback is never cached, and Stage 2 does not run on one:
      an empty `missing_criteria` from a real answer means "nothing is missing",
      from a fallback it would mean "nothing is known".
    - Nothing indexed for a payer short-circuits to the fallback without calling
      Bedrock at all. Asking Sonnet to answer from zero retrieved passages is an
      invitation to invent a policy.
    - The state filter is not an equality check. A policy ingested with no state
      applies nationally — every CMS national coverage determination is one — so
      the filter is `payer AND (state = X OR state IS NULL)`. An equality filter
      would hide every national policy and look exactly like "nothing indexed".
    - The cache key uses the request values verbatim, and `state`/`cpt_code` are
      uppercased in the request model the same way ingestion uppercases them.
      The same values build the Qdrant filter, so a key and the retrieval it
      stands for cannot disagree about which payer they mean.
    - `procedure` is in the prompt and the embedded query but *not* in the cache
      key, which is keyed on the CPT code. Two transcript labels for one code
      therefore share an entry, and whichever label produced the miss is the one
      that built the query. The prompt names the code as authoritative and the
      label as a hint for that reason. Worth knowing before assuming the label
      changes the answer.
    - Redis failures degrade rather than fail: a read error is a miss, a write
      error is an uncached answer. The dependency is still on `/health`, because
      paying Bedrock for every query is an outage worth naming.
    - The query route lives in `api/query.py` rather than alongside ingest in
      `api/policies.py`. Both mount `/policies`, but one must write an audit row
      and the other must not, and each module carries an AST-level test
      asserting its own direction. One module would have made that pair of
      standing decisions a comment.
    - No new environment variable. `REDIS_URL`, `AWS_REGION` and
      `BEDROCK_MODEL_ID_REASONING` were already in `.env.example`, unused.
    - The boto3/botocore mypy suppression moved from `packages/crypto-utils`'s
      import site to the root `pyproject.toml` overrides, which is what that
      file's own comment asked for once a second package started calling AWS.
    - CI needed no change: the test job already sets `REDIS_URL`, `AWS_REGION`
      and dummy AWS credentials, and starts redis from docker-compose. Note that
      moto's `bedrock-runtime` backend returns an *empty* body from
      `invoke_model`, so it can verify the client is built and addressed
      correctly and cannot stand in for a completion — the retry, fallback and
      parsing paths are tested against a stubbed `invoke_reasoning`.

- [ ] **TASK-013:** Policy scraper (background CronJob)
  - Service: `services/policy-scraper`
  - Scrape CMS Medicare coverage database (NCD/LCD pages) — public, no auth required
  - Respect the source: set a real User-Agent identifying this as MedAuth AI's scraper
    with a contact email, add a delay between requests (1-2s), check for and honor
    robots.txt. This is a research/dev-stage scraper against a public government
    database, not a high-volume crawler — no need to parallelize aggressively.
  - Download PDFs, hash content (SHA-256, matching TASK-011's content_hash), skip
    if hash matches existing record for that policy_id
  - Trigger ingestion via the TASK-011 `/policies/ingest` endpoint (internal call,
    not duplicating ingestion logic here)
  - Kubernetes CronJob manifest: runs nightly at 2am UTC
  - **Test:** run against CMS sandbox/public pages, verify at least one policy is
    downloaded + hashed. If CMS's page structure changes and this test starts
    failing, that's a real signal the scraper needs updating — don't loosen the
    test to mask it.

- [ ] **TASK-014:** Seed Qdrant with real payer policies for dev
  - `scripts/seed-policies.py`
  - Download publicly available policy PDFs from: CMS (Medicare), Aetna, BCBS (public guidelines)
  - Ingest into local Qdrant via TASK-011 `/policies/ingest` endpoint — reuses the
    same dedup logic, so re-running this script is safe and idempotent
  - Focus on: orthopedic MRI (CPT 72148), knee arthroscopy (CPT 29881), biologic injections

- [ ] **TASK-015:** Da Vinci CRD/DTR client — two-tier policy lookup
  - Service: `services/track-b-rag`
  - Background: CMS-0057-F mandates Medicare Advantage, Medicaid managed care,
    CHIP, and ACA marketplace payers expose standardized FHIR-based prior
    authorization APIs (Da Vinci CRD for "is auth required / what's needed,"
    DTR for the actual documentation questionnaire) by January 1, 2027. This
    does NOT cover commercial employer-sponsored plans, which remain the RAG
    pipeline's primary job — CRD/DTR is an enhancement for the payers it
    applies to, not a replacement for TASK-010 through TASK-014.
  - Testable now, not blocked on 2027: the HL7 CRD Reference Implementation
    (github.com/HL7-DaVinci/CRD) is a real, spec-conformant simulated payer
    server you run locally via Docker — add it to docker-compose.yml alongside
    the existing HAPI FHIR server. The HL7 CDS-Library
    (github.com/HL7-DaVinci/CDS-Library) provides sample coverage-requirement
    rule sets to load into it for realistic test scenarios. ONC's Inferno
    (inferno.healthit.gov, CRD and DTR test kits) is the federal conformance
    tester — running against it validates you're building to the actual
    certification bar, not an approximation of it.
  - `is_crd_supported(payer: str) -> bool` — a small config-driven lookup (start
    with a hardcoded dict of known-mandated payer/plan-type combos; this becomes
    real payer capability data over time, not something to over-engineer now)
  - Modify TASK-012's `/policies/query` to try CRD first when
    `is_crd_supported()` is true: call the payer's CRD endpoint via a CDS
    Hooks request (order-select or order-sign hook), and if it returns
    documentation requirements, use those directly — skip the RAG/Qdrant/Sonnet
    path entirely for that call. On any CRD failure (timeout, unsupported
    service, malformed response) or when `is_crd_supported()` is false, fall
    through to the existing RAG path unchanged. The response model returned by
    `/policies/query` stays identical either way — callers (TASK-021, TASK-040)
    never know or care which path answered.
  - **Test:** stand up the CRD Reference Implementation locally, load a sample
    rule from CDS-Library, query `/policies/query` for a payer marked
    `is_crd_supported=True`, verify the CRD path is used and RAG/Bedrock is not called
  - **Test:** query for a payer marked `is_crd_supported=False`, verify RAG path
    used as before (regression test — this task must not change existing behavior
    for unsupported payers)
  - **Test:** simulate a CRD timeout/error, verify fallback to RAG succeeds and
    the caller gets a normal response, not an error

---

## Phase 2 — Audio Pipeline

- [ ] **TASK-020:** Audio ingestion WebSocket server
  - Prerequisite: TASK-006 (session lifecycle) — this task validates the JWT
    that TASK-006 mints, it does not mint or manage sessions itself
  - Service: `services/audio-ingestion`
  - `WebSocket /ws/audio/{session_id}` — accepts raw audio chunks from client
  - Validate the JWT from the `Authorization` header against `JWT_SIGNING_KEY`
    before accepting the connection: verify signature, verify `exp` not passed,
    verify the token's `session_id` claim matches the URL's `session_id`. Close
    with code 4401 on any failure — do not accept the connection first and
    validate after.
  - Buffer chunks in in-memory BytesIO — never write to disk
  - Forward stream to AWS Transcribe Medical streaming API
  - On transcript segment received: publish to Redis channel
    `transcription:{session_id}` per CLAUDE.md's Redis key list
  - On disconnect: explicitly clear BytesIO buffer, close Transcribe stream
  - **Test:** send 10 seconds of test audio WAV chunks with a valid JWT, verify
    transcript events in Redis
  - **Test:** connect with an expired or malformed JWT, verify connection closes
    with 4401 and no Transcribe stream is opened

- [ ] **TASK-021:** Transcription event fan-out
  - Prerequisite: TASK-020 (publishes the events this task consumes), TASK-006
    (session lifecycle — Track A's consumer needs to know when to start
    accumulating and when the session it's tracking has ended)
  - This is two separate consumers in two separate services, not one shared
    fan-out component — each subscribes to the same Redis channel independently:
    - Track A consumer lives in `services/track-a-clinical` (implemented as
      part of TASK-030) — accumulates full transcript per session_id
    - Track B consumer lives in `services/track-b-rag` — scans for procedure
      order keywords, implemented in this task
  - Subscribe to `transcription:{session_id}` per CLAUDE.md's Redis key list
  - Keyword detection list: MRI, CT scan, X-ray, biopsy, injection, arthroscopy,
    echocardiogram, stress test, biologic, chemotherapy, referral to [specialist]
  - On keyword detected: extract surrounding context (the sentence or two
    containing the keyword), call TASK-012's `/policies/query` endpoint with
    that context as `clinical_context`, plus the `session_id` and `provider_id`
    for the active encounter — TASK-012 audits this call as a PHI access and
    needs both for the audit row
  - **Test:** publish transcript with "let's order an MRI" — verify policy
    query fires with the correct extracted context

- [ ] **TASK-022:** Mobile audio capture (React Native)
  - App: `apps/mobile`
  - `useAudioCapture` hook using expo-av
  - Records at 16kHz mono (required for Transcribe Medical)
  - Streams 250ms chunks over WebSocket to audio-ingestion service
  - Handles microphone permission request
  - Stops and clears on session end
  - **Test:** unit test the hook with mocked expo-av

- [ ] **TASK-023:** Browser audio capture (React Web)
  - App: `apps/web`
  - `useAudioCapture` hook using MediaRecorder API
  - Same 16kHz mono, 250ms chunks, WebSocket stream
  - **Test:** jsdom mock of MediaRecorder

---

## Phase 3 — Clinical Note Generation (Track A)

- [ ] **TASK-030:** Transcript accumulation + SOAP generation
  - Prerequisite: TASK-006 (session-end signal), TASK-020/021 (transcript events)
  - Service: `services/track-a-clinical`
  - Subscribe to `transcription:{session_id}` — accumulate segments into a rolling
    transcript buffer per session, in-memory (not persisted mid-session; the
    session is short enough that a service restart mid-encounter is an accepted
    edge case for v1, not something to build recovery for yet)
  - Subscribe to `session:ended:{session_id}` (published by TASK-006) — on
    receipt, call Claude **Sonnet** via Bedrock with the full accumulated
    transcript (see CLAUDE.md "Bedrock Model Assignment" — SOAP generation is
    Sonnet; the ICD/CPT extraction pass below is Haiku)
  - Prompt: generate SOAP note. Separately, run a Haiku extraction pass for
    ICD-10 codes and anticipated CPT codes — two calls, not one, so the cheap
    model handles the mechanical extraction and the expensive model focuses on
    the clinical writing
  - Store result in `clinical_notes` table
  - **Test:** send sample orthopedic encounter transcript, verify SOAP structure returned
  - **Test:** publish `session:ended:{session_id}` with an accumulated transcript
    buffered from prior TASK-020 test data, verify clinical_notes row is created

- [ ] **TASK-031:** Comprehend Medical validation layer
  - After LLM extracts ICD-10 codes (TASK-030's Haiku pass), validate with AWS
    Comprehend Medical InferICD10CM
  - Flag any codes LLM returned that Comprehend Medical did not confirm (confidence < 0.8)
  - Log discrepancies for quality monitoring — via standard `logging`, not
    hipaa-logger (this is a quality metric, not a PHI access event; log the
    code and confidence score, not the surrounding clinical text)
  - **Test:** run on transcript with 3 clear diagnoses, verify codes match

- [ ] **TASK-032:** SOAP note review endpoint
  - `GET /notes/{encounter_id}` — return generated SOAP note for provider review
  - `PATCH /notes/{encounter_id}` — provider edits, sets `provider_edited = true`
  - All accesses logged via hipaa-logger

---

## Phase 4 — Live Nudge System

- [ ] **TASK-040:** Nudge emitter
  - Service: `services/track-b-rag`
  - When `/policies/query` (TASK-012) returns `missing_criteria` non-empty or
    `denial_risk == "high"`: publish nudge to `nudges:{session_id}` per
    CLAUDE.md's Redis key list
  - Nudge payload: `{type: "PAYER_RULE_ALERT", procedure, cpt_code, message,
    missing_criteria, denial_risk, haptic: bool}` — `haptic` is true only when
    `denial_risk == "high"`
  - Store nudge in `clinical_nudges` table (schema in TASK-005), get back the
    row `id` — include it in the Redis payload as `nudge_id` so the client can
    later acknowledge this specific nudge
  - **Test:** trigger policy query with known missing criteria, verify Redis pub
    includes a valid nudge_id matching the stored row

- [ ] **TASK-041:** Nudge WebSocket relay
  - Prerequisite: TASK-006 (JWT), same auth pattern as TASK-020
  - Service: `services/nudge-service`
  - `WebSocket /ws/nudges/{session_id}` — same JWT validation as TASK-020
    (verify signature, exp, session_id claim match; 4401 on failure)
  - Subscribe to `nudges:{session_id}`
  - Forward each nudge event to connected client in real time
  - On client disconnect: unsubscribe from Redis channel
  - **Test:** publish nudge to Redis, verify it appears at WebSocket client
  - **Test:** connect with invalid JWT, verify 4401 close

- [ ] **TASK-041b:** Nudge acknowledge endpoint
  - Service: `services/track-b-rag` (owns the clinical_nudges write from TASK-040)
  - `PATCH /nudges/{nudge_id}/acknowledge` — sets `acknowledged=true`,
    `acknowledged_at=NOW()`. This is what TASK-042's dismiss button calls —
    it was referenced by the UI task but never specified as its own endpoint
    until now.
  - **Test:** acknowledge a nudge, verify row updated

- [ ] **TASK-042:** Nudge UI component (web)
  - App: `apps/web`
  - `<NudgeOverlay sessionId={...} />` — subscribes to nudge WebSocket
  - High-contrast banner, color-coded by denial_risk (yellow/orange/red)
  - Dismiss button calls `PATCH /nudges/{nudge_id}/acknowledge` (TASK-041b)
    using the `nudge_id` included in the WebSocket payload
  - Accessible (ARIA role=alert, focus management)
  - **Test:** render with mock WebSocket, verify alert appears on message
  - **Test:** click dismiss, verify acknowledge endpoint is called with correct nudge_id

- [ ] **TASK-043:** Haptic nudge (mobile)
  - App: `apps/mobile`
  - On nudge received with `haptic: true`: call `Haptics.notificationAsync()`
  - Same visual alert as web, same dismiss-calls-acknowledge behavior (TASK-041b)
  - **Test:** mock WebSocket message, verify Haptics mock called

---

## Phase 5 — FHIR Integration

Build the adapter layer first (TASK-050), then implement base functionality
against the Athenahealth sandbox (TASK-051 through TASK-053). Each subsequent
EHR only requires adding a subclass and any overrides — the routes and core
logic do not change.

- [ ] **TASK-050:** EHR adapter layer scaffold
  - Service: `services/fhir-integration` (Python 3.12 + FastAPI — same as all other services)
  - Create `src/adapters/base.py` — EHRAdapter base class with all core FHIR methods as stubs
  - Create `src/adapters/factory.py`:
    - `detect_ehr_from_issuer(iss_url: str) -> str` — maps known EHR base URLs to vendor keys
      (check for "epic", "cerner", "oraclehealth", "athena", "eclinicalworks", "modmed" in URL)
    - `get_adapter(ehr_type, fhir_base_url, access_token) -> EHRAdapter`
  - Create empty subclass files: `athena.py`, `ecw.py`, `modmed.py`, `cerner.py`, `epic.py`
  - The route handlers import only `get_adapter()` and `detect_ehr_from_issuer()` — never import
    a specific adapter class directly in routes
  - **Test:** unit test detect_ehr_from_issuer() with sample URLs from each vendor

- [ ] **TASK-051:** SMART on FHIR OAuth flow (EHR-agnostic)
  - Service: `services/fhir-integration`
  - `GET /fhir/launch` — receives `launch` + `iss` params from EHR
    - Calls `detect_ehr_from_issuer(iss)` to identify vendor
    - Fetches `{iss}/.well-known/smart-configuration` to discover auth + token endpoints
    - Stores iss + ehr_type in Redis session keyed by state param
    - Redirects to EHR's authorization_endpoint
  - `GET /fhir/callback` — receives auth code, exchanges for access token
    - Retrieves session from Redis using state param
    - POSTs to EHR's token_endpoint to exchange code
    - Stores access token + fhir_base_url + ehr_type in Redis (TTL = token expiry)
    - Returns session_id to client for subsequent API calls
  - Supports both EHR launch (EHR opens MedAuth in iframe) and standalone launch
  - **Test:** mock authorization server returning code + token, verify full flow
  - **Test:** verify state mismatch returns 400 (CSRF protection)

- [ ] **TASK-052:** Base FHIR resource fetching (implements base.py methods)
  - Service: `services/fhir-integration`
  - Implement in `adapters/base.py` — these use standard US Core FHIR only:
    - `get_patient(patient_id)` → normalized PatientContext
    - `get_coverage(patient_id)` → normalized CoverageInfo (payer, plan, member_id)
    - `get_conditions(patient_id)` → list of active Condition
    - `get_encounter(encounter_id)` → Encounter
  - Routes (all vendor-agnostic — use get_adapter() from session ehr_type):
    - `GET /fhir/patient/{patient_id}/context` — returns PatientContext (patient + coverage + conditions)
    - `GET /fhir/encounter/{encounter_id}` — returns Encounter
  - All accesses logged via hipaa-logger
  - If Coverage resource returns incomplete payer info: set `requires_manual_confirmation: true`
    in response — do not fail, let provider fill it in
  - **Test:** against local HAPI FHIR loaded with Synthea patients — verify all fields populated
  - **Test:** against Athenahealth sandbox with real sandbox credentials

- [ ] **TASK-053:** SOAP note write-back (base.py)
  - Implement `write_clinical_note(encounter_id, note_text, icd10_codes)` in base.py
  - Creates FHIR DocumentReference resource (LOINC 11488-4 — Consult note)
  - Route: `POST /fhir/notes`
  - Updates `clinical_notes.ehr_document_ref_id` on success
  - **Test:** write to local HAPI FHIR, verify DocumentReference created + stored
  - **Test:** against Athenahealth sandbox

- [ ] **TASK-054:** Prior auth submission — base + Athena override
  - Implement `submit_prior_auth(bundle)` in base.py using FHIR Claim/$submit (Da Vinci PAS)
  - Override in `adapters/athena.py`: Athenahealth does not support FHIR PAS — override
    to submit via CoverMyMeds API instead
  - Route: `POST /fhir/prior-auth`
  - The route handler calls `adapter.submit_prior_auth()` — it gets the right path automatically
  - Records payer reference number + submission_method in prior_auth_requests table
  - **Test (base):** submit to local HAPI FHIR as mock payer
  - **Test (Athena override):** mock CoverMyMeds API, verify it's called instead of FHIR PAS

- [ ] **TASK-055:** Athenahealth adapter completion
  - All TASK-052 through TASK-054 work against Athenahealth sandbox
  - Validate every FHIR resource response against real Athenahealth sandbox data
  - Document any Athenahealth-specific quirks in `adapters/athena.py` docstring
  - Get sandbox credentials from developer.athenahealth.com — add to .env.example
  - This is the first fully operational EHR integration — treat it as the v1 milestone

- [ ] **TASK-056:** Cerner adapter
  - Prerequisite: TASK-055 complete (Athenahealth working in production pilot)
  - Register at code.cerner.com, get sandbox credentials
  - Implement CernerAdapter(EHRAdapter) — override get_patient_context() to handle
    incomplete Coverage resource (add requires_manual_confirmation fallback)
  - Validate all resources against Cerner sandbox
  - Add "cerner" detection to factory.py detect_ehr_from_issuer()
  - **Test:** full integration test suite against Cerner sandbox

- [ ] **TASK-057:** Epic adapter
  - Prerequisite: at least 3 paying customers on Athenahealth or Cerner
  - Register at open.epic.com, work through App Orchard review process
  - Implement EpicAdapter(EHRAdapter) — override get_patient_context() to optionally
    enrich with Epic proprietary extensions (preferred language, scheduling context)
  - All Epic extensions are additive — base functionality still works without them
  - Add "epic" detection to factory.py
  - **Test:** full integration test suite against Epic sandbox at fhir.epic.com
  - Note: Epic production access requires App Orchard approval + reference customer

- [ ] **TASK-058:** Modernizing Medicine (EMA) adapter
  - Prerequisite: TASK-055 complete
  - High priority despite lower market share — EMA targets dermatology + orthopedics specifically
  - Register at developer.modmed.com
  - Implement ModMedAdapter(EHRAdapter)
  - **Test:** validate against EMA sandbox

---

## Phase 6 — Prior Auth Bundle Assembly

- [ ] **TASK-060:** Bundle assembler
  - Prerequisite: TASK-006 (session:ended signal), TASK-030 (clinical_notes must
    exist before this runs), TASK-040 (clinical_nudges), TASK-052 (FHIR patient data)
  - Service: `services/prior-auth`
  - Subscribe to `session:ended:{session_id}` per CLAUDE.md's Redis key list
  - On receipt: fetch clinical_note (by encounter's session_id), nudges fired
    during the encounter, and patient FHIR context (via fhir-integration's
    `/fhir/patient/{patient_id}/context` from TASK-052)
  - Assembles `prior_auth_bundle`: patient, provider, procedures (from nudges'
    procedure_name/cpt_code), diagnoses (from clinical_note's icd10_codes),
    clinical evidence (relevant transcript excerpts — not the full transcript,
    just the segments tied to flagged procedures)
  - Stores in `prior_auth_requests` table with status = 'pending'
  - Note: this task has a soft race condition — if session:ended fires before
    TASK-030's SOAP generation finishes (Sonnet call can take a few seconds),
    the clinical_note won't exist yet. Handle by retrying with backoff (3
    attempts, 2s/4s/8s) before giving up and logging a warning — do not fail silently.
  - **Test:** assemble bundle from test encounter data, verify all fields populated
  - **Test:** publish session:ended before clinical_notes row exists, verify retry
    behavior succeeds once the row appears

- [ ] **TASK-061:** Submission router
  - Service: `services/prior-auth`
  - `POST /prior-auth/{request_id}/submit`
  - Checks payer capabilities: supports FHIR PAS? → calls fhir-integration's
    `/fhir/prior-auth` (TASK-054, which itself routes to the right adapter).
    Otherwise → CoverMyMeds API directly from this service.
  - Updates `prior_auth_requests.status` and `submission_method`
  - **Test:** mock both submission paths, verify correct one chosen per payer config

---

## Phase 7 — Provider Dashboard (Web App)

- [ ] **TASK-070:** Session management UI
  - App: `apps/web`
  - Start session: patient search via `GET /fhir/patient/search?query=...` (add
    this search route to fhir-integration if not already covered by TASK-052 —
    flag if it's missing rather than assuming it exists), then
    `POST /sessions/start` (TASK-006) with the selected patient, begin audio
    capture (TASK-023) once session_id + jwt are returned
  - Active session view: live transcript display (subscribe to a display-only
    read of `transcription:{session_id}` — reuse the nudge-service WebSocket
    pattern from TASK-041 rather than inventing a new relay), `<NudgeOverlay>`
    (TASK-042), and a simple checklist of flagged procedures pending documentation
  - End session: calls `POST /sessions/{session_id}/end` (TASK-006), stops
    audio capture, navigates to the note review screen (TASK-071)
  - **Test:** component tests for start/active/end state transitions with mocked APIs

- [ ] **TASK-071:** Note review + edit UI
  - App: `apps/web`
  - `GET /notes/{encounter_id}` (TASK-032) — display generated SOAP note in an
    editable form, one text area per SOAP section
  - Provider can edit any section; "Save" calls `PATCH /notes/{encounter_id}`
    (TASK-032), which sets `provider_edited = true`
  - "Write to EHR" button calls fhir-integration's `POST /fhir/notes` (TASK-053)
  - Show ICD-10 and CPT codes extracted (from clinical_notes.icd10_codes /
    cpt_codes), editable as a tag-style list — allow add/remove, save via the
    same PATCH endpoint
  - **Test:** load a note, edit a section, save, verify PATCH called with correct diff

- [ ] **TASK-072:** Prior auth status dashboard
  - App: `apps/web`
  - `GET /prior-auth?status=` (add this list endpoint to `services/prior-auth`
    if not already covered — flag if missing) — list all prior auth requests
    with status (pending, submitted, approved, denied)
  - Denial reason display (`prior_auth_requests.denial_reason`)
  - Resubmission flow for denied requests: calls `POST /prior-auth/{request_id}/submit`
    again (TASK-061) — same endpoint, submission_method may differ if the first
    attempt's method is known to have failed
  - **Test:** render list with mixed statuses, verify denial reason shown only
    for denied items, verify resubmit button only shown for denied items

---

## Known Constraints for Claude Code

1. **Do not install Kafka.** Redis pub/sub is the message bus. Any task that says "publish event" means `redis.publish()`.

2. **AWS Bedrock, not direct Anthropic API.** All LLM calls use `langchain_aws.ChatBedrock` with `boto3.client('bedrock-runtime')`. Never import `anthropic` directly.

3. **Moto for all AWS mocking in tests.** `@mock_aws` decorator on test functions that call Bedrock, Transcribe Medical, Comprehend Medical, KMS.

4. **Pydantic v2 syntax.** `model_config = ConfigDict(...)` not `class Config:`. `model_validate()` not `parse_obj()`.

5. **SQLAlchemy 2.0 async style.** `async with async_session() as session:` pattern. No sync session calls.

6. **Every new API route needs:** (a) Pydantic request/response models, (b) an
   OpenAPI docstring, (c) at least one integration test, and (d) a hipaa-logger
   `audit_log()` call **if and only if the route touches PHI**.
   The audit_log table is a compliance artifact, not a general write log — its
   value in an audit comes from every row being a PHI access. Diluting it with
   operational events makes "who accessed patient X" a query you have to filter
   rather than just run. Routes that touch no PHI use standard
   `logging.getLogger(__name__)` at INFO instead — you still get the operational
   trace, in the right place.
   Known non-PHI routes as of now: `/health` on every service, and
   `/policies/ingest` (TASK-011 — insurance policies are public payer
   publications with no patient linkage). Judge new routes by the same test
   rather than adding to this list reflexively; if a route's PHI status is
   genuinely unclear, flag it rather than guessing either way.

7. **TASK numbers must be recorded in commit messages.** Format: `feat(track-b-rag): implement policy query endpoint [TASK-012]`, each commit must follow the Git 50/72 commit rule (See CLAUDE.md for more details).

8. **Session lifecycle is centralized in TASK-006.** Nothing else mints or validates
   session JWTs independently — audio-ingestion, nudge-service, and any future
   real-time endpoint all validate against the same `POST /sessions/start`-issued
   token. Do not build a parallel auth mechanism for convenience.

9. **Two endpoints are flagged as possibly-missing, not assumed-present:**
   FHIR patient search (`GET /fhir/patient/search`, needed by TASK-070) and
   prior-auth list (`GET /prior-auth?status=`, needed by TASK-072). If you reach
   either task and the endpoint doesn't exist yet, add it as part of that task
   rather than assuming it was built elsewhere — flag it back if the scope is
   unclear, same as any other gap found so far.
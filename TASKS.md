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
    Also publishes the new `session_id` to the fixed `sessions:started`
    channel — added in TASK-021, which needs it to subscribe to one session's
    transcript channel by name instead of pattern-subscribing across all of
    them. Published before the response, so no client can be speaking before a
    consumer is listening; a failed publish is a 503, because a session nobody
    watches raises no nudges and looks like a session with nothing to flag.
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
    - **TASK-021 added the `sessions:started` publish** to `/sessions/start`,
      and with it a 503 path this endpoint did not previously have. The two
      publishes are not symmetric and should not be made so: the end signal is
      empty because its channel names the session, while the start signal
      carries `{"session_id": ...}` in its payload because its channel is fixed
      and cannot. The known gap noted above — a broker failure after the commit,
      with no durable outbox — now applies to both ends of the lifecycle.

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
  - **Extended for TASK-013: the route takes HTML as well as PDF.** CMS does
    not publish LCDs or NCDs as PDFs at all — the Medicare Coverage Database's
    "PDF" affordance is the browser's own print-to-PDF, and the bulk export
    carries the document body as HTML fragments in CSV fields — so the scraper
    had nothing this route would accept. What changed, and what did not:
    - One `content_type` form field, `application/pdf` (the default) or
      `text/html`, declared by the caller rather than sniffed. A caller always
      knows what it fetched, and a wrong guess would silently index the wrong
      parse. It stays the sole body parameter's field, so FastAPI keeps
      flattening the form — the shape the note above warns against breaking.
    - `documents.py` now owns the format-independent half: `content_digest()`
      moved there from `pdf.py`, and `extract_text()` dispatches to `pdf` or the
      new `markup` module. `PdfParseError` and `HtmlParseError` both derive from
      `DocumentParseError`, so the route maps any reader's failure to 400
      without knowing which formats exist.
    - The error code is now `invalid_document`, renamed from `invalid_pdf`:
      answering "invalid_pdf" to a malformed HTML upload sends a scraper author
      looking at the wrong thing. Both callers are internal, so nothing
      published depended on the old spelling.
    - HTML extraction is stdlib `html.parser`, not BeautifulSoup — the job is
      prose out of `<p>`/`<ul>`/`<table>` with block boundaries kept as blank
      lines, and that does not justify a dependency in this service. It reads
      *fragments*: what the export carries is a section body, not a page with
      `<html>` around it. UTF-8 first, then cp1252, because payer documents
      carry smart quotes and a strict-UTF-8 reader would reject a policy over
      its apostrophes.
    - **Chunking is unchanged, deliberately.** `chunk_size=800`,
      `chunk_overlap=150` apply to extracted HTML exactly as to extracted PDF:
      both readers hand `chunk_text()` plain text with blank lines at block
      boundaries, and policy prose is not shaped differently for having been
      published as HTML.
    - **The digest still covers the raw uploaded bytes**, which is what makes
      the HTML path safe to re-scrape nightly. Rendering HTML to a PDF so this
      route could stay PDF-only was rejected on measurement: PyMuPDF's output is
      not byte-deterministic, so the same document rendered twice yields two
      digests, every scrape reads as an update, and the corpus re-embeds daily.
      The live MCD page is not byte-stable either — it carries a per-request CSP
      nonce — so a live-page fallback would have to hash extracted text, never
      the HTTP body.
    - TASK-011's own tests were extended rather than duplicated: the three dedup
      claims (created, unchanged, updated) are parametrised over both formats in
      `tests/integration/test_ingestion.py`, so the HTML path proves them
      instead of inheriting them. 411 tests, 100% coverage.

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
    function that TASK-015 can join the Da Vinci CRD tier onto, leaving this RAG
    implementation unchanged. Same response shape either way; callers never
    branch on which path answered. Cheaper to define the seam now than to
    refactor one task later. (What TASK-015 actually did with it: CRD does not
    front this function, it runs beside it. CRD carries no documentation
    criteria — the IG delegates those to DTR — so it decides `requires_auth`
    while this implementation still supplies `auth_criteria` and the step
    therapy fields. The seam was still the right thing to build; only the
    expectation that CRD would replace it wholesale was wrong. See TASK-015.)
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

- [x] **TASK-013:** Policy scraper (background CronJob)
  - Service: `services/policy-scraper`
  - Prerequisite: **TASK-016** (`packages/payer-vocab`). Everything this task
    writes is keyed on a canonical payer slug — CMS documents ingest as
    `cms-medicare`, never as "CMS" or "Medicare".
  - **Package rename, do this first** — the same move TASK-010 made for
    track-b-rag: rename to `src/policy_scraper/` and declare
    `medauth-track-a-clinical` as a dependency. This service reads
    `insurance_policies.content_hash` before uploading, which crosses the
    service-import boundary. The service is still empty, so the rename is free
    now and churn later.

  **Source: the MCD bulk exports, not a page crawl.** This replaces the original
  "scrape NCD/LCD pages", which was written before anyone checked what CMS
  actually publishes.
  - `https://downloads.cms.gov/medicare-coverage-database/downloads/exports/`
    serves `ncd.zip` (~1 MB), `current_lcd.zip` (~32 MB) and
    `current_article.zip` (~41 MB), each a set of CSV tables with a published
    data dictionary, regenerated daily at about 02:00 UTC.
  - **The export carries the full document text, so there are no per-document
    fetches at all** — three archive downloads a night, against crawling 949 LCD
    pages. `lcd.csv` holds the body in `indication`, `summary_of_evidence`,
    `analysis_of_evidence`, `associated_info`, `cms_cov_policy`, `issue` and
    `bibliography`, one row per LCD; `ncd_trkg.csv` holds it in `itm_srvc_desc`
    and `indctn_lmtn` across 357 NCDs. Verified against the rendered page for
    L39529: 45 of the 46 sentences in its coverage-criteria section appear
    verbatim in `indication`, the 46th being the section heading. What the page
    has and the export does not is AMA/AHA licence boilerplate and navigation
    chrome — noise this pipeline is better off without.
  - **Schedule the CronJob at 03:30 UTC, not 02:00.** CMS regenerates the
    exports at 02:00 UTC (confirmed from `Last-Modified`); running at exactly
    that hour races the regeneration for a stale or half-written file.
  - Still send a real User-Agent naming MedAuth AI with a contact email, keep a
    1-2s delay between requests, and fetch and honour robots.txt. Three verified
    facts to build against:
    - `www.cms.gov/robots.txt` carries `Disallow: /*?` but an explicit
      `Allow: /medicare-coverage-database/*?` ahead of it. Under the
      longest-match rule the MCD's query-string URLs are permitted; a naive
      "does any Disallow match" check would wrongly refuse the entire database.
    - There is no `Crawl-delay` directive, so the 1-2s delay is our own
      politeness policy rather than a published requirement. Keep it anyway.
    - `downloads.cms.gov` serves no robots.txt at all. Treat missing or
      unparseable as allow, and keep the delay regardless. cms.gov also answers
      403 to some clients based on User-Agent, so the UA is load-bearing rather
      than decoration.

  **Scope (settled): a curated CPT/HCPCS filter, not the whole database.**
  - Filter to the codes MedAuth actually targets. Against the current export
    that resolves to **18 LCDs** — few enough to be polite, specific enough to
    be a real dev corpus.
  - Keep the list in one module-level constant with a comment per code saying
    why it is there. Start from: 72148/72149 (lumbar spine MRI), 73721/73718
    (lower-extremity joint MRI), 20610 (major joint injection), J7321/J7325
    (hyaluronan knee injections), 64483/62323 (epidural steroid injections),
    Q5121 (infliximab biosimilar).
  - **Two corrections to earlier drafts, both load-bearing:**
    - 72148 is MRI of the *lumbar spine*, not a knee MRI. Knee MRI without
      contrast is 73721. Both belong in the list — the label was wrong, not the
      code.
    - **29881 (knee arthroscopy with meniscectomy) has no CMS coverage document
      at all** — no LCD, no article. Neither do the dermatology biologics
      (Cosentyx, Taltz, Skyrizi, Stelara, Dupixent). They are not missing from
      our filter, they are absent from Medicare's coverage database, because
      they are commercial and pharmacy-benefit territory. Do not widen the CMS
      filter hunting for them; that is TASK-014's job, against Aetna and BCBS.
  - **The code-to-document index lives in the Articles, not the LCDs.** CMS moved
    code lists out of LCDs into companion Billing & Coding Articles, so only 66
    LCDs (all DME) carry inline HCPCS codes and essentially no physician CPT
    codes at all. The join is `article_x_hcpc_code` →
    `article_related_documents` → `lcd_id`, and then fetch that LCD. Filtering
    `lcd_x_hcpc_code` alone matches almost nothing, and looks exactly like a
    working scraper that found no work to do.

  **LCD jurisdiction (settled): one document carrying a list of states.**
  - An LCD is issued per MAC jurisdiction and applies across it — a median of 12
    states over the 949 current LCDs, up to 48 for the widest. Resolve with
    `lcd_x_contractor` → `contractor_jurisdiction` (skipping rows that have a
    `term_date`) → `state_lookup`, then normalise every code to a USPS state per
    CLAUDE.md. CMS's `state_abbrev` includes `CNMI`, `DN`, `QN`, `UN`, `NF`,
    `SF`, `EM` and `WM` — none of which a FHIR `Coverage` will ever produce, and
    one of which does not even fit `CHAR(2)`.
  - **Qdrant:** one set of chunks per LCD, with the payload `state` holding the
    *list* of normalised states. This needs no change to the retrieval filter —
    `MatchValue` matches any element of a list-valued payload, verified against
    the running Qdrant using `policy_query_filter` itself, both with and without
    the keyword payload index TASK-011 creates. NCDs stay `state: null` and go
    on matching every state through the existing `IsNullCondition`.
  - **Postgres:** one row per LCD, `policy_id` of the form `cms-lcd-L39529`. Add
    a nullable `jurisdiction_states TEXT[]` column to `insurance_policies` for
    the resolved list. `state CHAR(2)` stays for genuinely single-state
    documents (the commercial plan policies TASK-014 ingests) and NULL keeps
    meaning national. The migration is authored in track-a-clinical per the
    migration-ownership rule, even though this service writes the rows.
  - Rejected: one row per state with a composite `policy_id`
    (`cms-lcd-L39529-MA`). It turns 18 documents into 230 rows, and worse,
    duplicates identical policy text about 12 times over in Qdrant — 12x the
    embedding cost, and near-identical chunks crowding each other out of the
    eight retrieval slots.

  **Document format — this one changes TASK-011's ingest contract.**
  - CMS does not publish LCDs or NCDs as PDFs. The MCD's "PDF" affordance is the
    browser's own print-to-PDF, and the document body is HTML — carried as HTML
    fragments inside the export's CSV fields. `/policies/ingest` accepts PDF
    only, so this task cannot use it as written.
  - **Extend `/policies/ingest` to accept a declared content type** —
    `application/pdf` as today, plus `text/html` — and hash the bytes the payer
    published. That keeps `content_hash` meaning "the document the payer
    published", which is what TASK-011 says it means.
  - **What gets hashed, precisely:** the document's content fields from the
    export, concatenated in a fixed field order and encoded UTF-8, exactly as
    CMS serves them and with no reformatting. Volatile row metadata —
    `last_updated`, `lcd_version` — is *not* part of the digest; it describes the
    export, not the document, and folding it in would re-ingest a policy whose
    text never changed.
  - **Never hash a raw MCD page response.** Verified: fetching
    `view/lcd.aspx?lcdid=39529` twice, seconds apart, returns two responses of
    identical length whose SHA-256 digests differ — 478 bytes differ, all of them
    the per-request CSP `nonce` attribute on the page's script tags. A live-page
    digest would therefore change on every fetch and mark every policy `updated`
    every night, re-embedding the whole corpus. This is the same failure the
    rejected PDF-rendering option below has, arrived at from a different
    direction. If anyone ever adds a live-page fallback, it must hash extracted
    document text, never the HTTP body.
  - Rejected: rendering the HTML to PDF inside the scraper so ingest can stay
    PDF-only. PyMuPDF's output is not byte-deterministic — the same content
    rendered twice yields two different SHA-256 digests (verified). Every
    nightly run would then look like an update, for the same reason and with the
    same cost.
  - **The TASK-011 change is a discriminator, not a second pipeline.** One
    `content_type` field on `IngestPolicyRequest`, kept as the single body
    parameter so FastAPI still flattens the form — the shape that task's notes
    warn against breaking. `pdf.extract_text()` gains an HTML sibling
    (fragments, not whole documents — the export's fields are not full pages),
    and `ingest_policy()` dispatches on the declared type. Everything downstream
    is unchanged.
  - **Chunking is unchanged and this is deliberate:** `chunk_size=800`,
    `chunk_overlap=150` apply to extracted HTML text exactly as they do to
    extracted PDF text. Both paths hand `chunk_text()` a plain string, the
    constants are module-level for the reason `chunking.py` gives — changing
    either invalidates every chunk already indexed — and policy prose does not
    become differently shaped for having arrived as HTML.
  - **Update TASK-011's existing tests, do not append a parallel suite.** The
    cases that matter are in `tests/unit/test_pdf.py` (10),
    `tests/unit/test_ingestion.py` (18), `tests/unit/api/test_policies.py` (20)
    and `tests/integration/test_ingestion.py` (11). Each one that asserts
    PDF-specific behaviour becomes a case over both content types, so the three
    dedup claims — created, unchanged, updated — are proven on the HTML path
    too rather than inherited from the PDF path. `test_openapi_contract.py`
    compares `docs/api/track-b-rag.yaml` against the generated schema, so the
    spec is updated in the same change or that test fails.
  - Ingest remains the only thing that chunks, embeds or writes Qdrant. This
    service fetches, filters, resolves jurisdictions, and uploads.

  - Hash locally before uploading: read `insurance_policies.content_hash` and
    skip the upload when it matches. This is a bandwidth optimisation only.
    Ingest's own dedup stays the authority, and losing a race costs one
    redundant upload, never a wrong result.
  - No `audit_log()` call anywhere in this service — public payer publications,
    no patient linkage, the same call `/policies/ingest` makes. Log at INFO
    through `logging.getLogger(__name__)` instead.
  - Kubernetes CronJob manifest at 03:30 UTC. This is the first file under
    `infrastructure/kubernetes/`.
  - Env: `CMS_COVERAGE_DB_BASE_URL` and `POLICY_SCRAPER_USER_AGENT` already
    exist in `.env.example`, unused. Add `CMS_MCD_EXPORTS_BASE_URL` — the
    exports live on a different host from the database UI. Put the contact
    email inside the User-Agent value rather than adding a var for it.
  - **Test (always on):** the filter, the article-to-LCD join, jurisdiction
    resolution and state normalisation, against recorded fixture CSVs and one
    recorded LCD document. These carry the 80% coverage gate.
  - **Test (gated on `RUN_CMS_LIVE_TESTS=1`):** fetch the real exports, verify at
    least one policy is downloaded and hashed, and assert the CSV tables the
    join depends on still carry the columns it reads. If CMS's structure changes
    this must fail — do not loosen it to mask the drift.
  - **Nightly workflow:** add `.github/workflows/nightly-live-checks.yml`,
    scheduled with `RUN_CMS_LIVE_TESTS=1` set, plus `workflow_dispatch`. A gate
    with no scheduled run is a deleted test — see CLAUDE.md's section on this.
  - CI: `policy-scraper` is already in ci.yml's service matrix, so the per-PR job
    needs no change. The nightly workflow is the only new CI file.
  - Built (127 tests, 99% coverage; the live checks pass against CMS as of
    2026-08-22). Decisions worth knowing before touching this:
    - **robots.txt is parsed here rather than with `urllib.robotparser`.** That
      module's `RuleLine.applies_to` is a prefix comparison with no wildcard
      handling, so `Disallow: /*?` is treated as the literal prefix `/*?` and
      matches nothing. Against CMS's file it reaches the right answer for the
      wrong reason, which is worse than being wrong — the next site with a
      wildcard rule would be crawled in breach of it and nothing would say so.
      `policy_scraper.robots` implements longest-match with Allow winning ties,
      which is what makes the Medicare Coverage Database fetchable despite the
      site-wide query-string Disallow.
    - **A disallowed URL raises; it is never skipped quietly.** A run that
      fetched nothing because a rule changed would look exactly like a run with
      nothing to fetch.
    - The run makes **three requests plus one robots.txt per host**. There are no
      per-document fetches at all, so the 1–2s delay barely matters in practice
      — it is kept because the next source might need it.
    - **The digest covers the export's content fields in a fixed order**, which
      is why `LCD_FIELDS`/`NCD_FIELDS` are module constants rather than a dict
      iteration: reordering them would look like every policy changing at once.
      `last_updated` and `lcd_version` are excluded deliberately — they describe
      the export, not the policy.
    - **A title alone is not a document.** The emptiness check looks at the body
      fields, not at the assembled bytes; a row with a heading and nothing else
      would otherwise chunk into one heading and record a content hash against a
      vector that says nothing, which every later scrape then reports as
      "unchanged". A test caught this, not review.
    - The pre-upload digest lookup is **one query for the whole run**, not one
      per document, and it is an optimisation only — ingest's own dedup decides
      created/unchanged/updated, so losing a race costs one redundant upload.
    - **One failing document does not fail the run**, but any failure exits the
      process non-zero, so Kubernetes marks the job failed rather than reporting
      a green run that indexed nothing.
    - `.env.example` gains `CMS_MCD_EXPORTS_BASE_URL`: the exports are on
      `downloads.cms.gov` and the database UI is on `www.cms.gov`, and robots.txt
      is per host, so conflating them would check the wrong file.
    - The nightly workflow runs the live checks at 05:00 UTC — after the
      scraper's own 03:30 slot and well after CMS regenerates the exports at
      about 02:00, so a failure is about their structure rather than our timing.

- [x] **TASK-014:** Seed Qdrant with commercial payer policies for dev
  - `scripts/seed-policies.py`
  - **Aetna and BCBS only. CMS is not in scope for this task** — seed Medicare
    coverage with `uv run python -m policy_scraper` (TASK-013) instead. An
    earlier draft of this task listed CMS alongside the commercial payers, which
    was written before TASK-013 existed. CMS publishes no policy PDFs at all: the
    Medicare Coverage Database's "PDF" affordance is the browser's own
    print-to-PDF and the documents are HTML fragments inside the CSV exports,
    which `services/policy-scraper` already fetches, filters, resolves
    jurisdictions for, and ingests. A second CMS path here would duplicate that
    pipeline and the two would fight over the same `policy_id`s
    (`cms-lcd-L39529`), each overwriting the other's Qdrant points on every run.
  - Download publicly available policy PDFs from Aetna (Clinical Policy
    Bulletins) and BCBS (published medical policy / prior-authorization
    guidelines).
  - Ingest into local Qdrant via TASK-011 `/policies/ingest` endpoint — reuses the
    same dedup logic, so re-running this script is safe and idempotent
  - Focus on: orthopedic MRI (CPT 72148), knee arthroscopy (CPT 29881), biologic injections
  - Ingest under canonical payer slugs from `packages/payer-vocab` (TASK-016),
    not display names — `aetna`, not "Aetna Inc." See CLAUDE.md, "Payer and
    jurisdiction identity".
  - **Aetna publishes HTML, not PDF — verified, and this reverses an earlier
    note in this task.** Clinical Policy Bulletins are served as
    `Content-Type: text/html` from `aetna.com/cpb/medical/data/<range>/<id>.html`
    (checked live against CPB 0743, "Spinal Surgery: Laminectomy and Fusion",
    which carries CPT 72148, and CPB 0208). The original claim that "Aetna and
    BCBS do publish real PDFs" was inherited from a draft written before anyone
    fetched one. BCBS of Massachusetts does publish PDFs. So this script uses
    **both** of `/policies/ingest`'s content types: `text/html` for Aetna,
    `application/pdf` for BCBS. It is also where the codes CMS has no coverage
    document for — 29881, and the dermatology biologics — are actually covered,
    since those are commercial and pharmacy-benefit territory.
  - **This task's tests must cover the `text/html` ingest path.** The question
    was raised as "CMS is out of scope, so does this second caller of
    `/policies/ingest` still need HTML coverage?" — and the answer changed once
    Aetna's documents were actually fetched. It is not CMS that puts HTML in
    this script's path, it is Aetna. So the suite covers both content types from
    this caller, on top of the coverage the path already has at the endpoint
    (`services/track-b-rag/tests/unit/test_documents.py`,
    `tests/unit/test_ingestion.py`, `tests/unit/api/test_policies.py` and
    `tests/integration/test_ingestion.py` all parametrise over both) and from
    policy-scraper (`services/policy-scraper/tests/unit/test_ingest.py`).
  - **The policy URL list is a curated module-level constant, not a crawler.**
    One entry per document with a comment saying why it is there, the same shape
    as TASK-013's CPT/HCPCS constant. Aetna and BCBS publish per-plan,
    per-licensee documents behind no machine-readable index, so there is nothing
    to crawl and crawling would be the impolite way to find out. Both payers'
    policy indexes render their document lists in JavaScript — a static fetch of
    `aetna.com/health-care-professionals/clinical-policy-bulletins/...` returns
    zero CPB links, and BCBSMA's `/medical-policies/` listing yields one PDF href
    that 404s against every base it could resolve to. The URL list is therefore
    assembled by a human reading the index, not derived programmatically.
    Individual CPB URLs are stable and directly fetchable once known.
  - robots.txt on both `www.aetna.com` and `www.bluecrossma.org` permits the
    policy paths — checked with `policy_scraper.robots.RobotsPolicy`, not
    `urllib.robotparser`, for the wildcard-handling reason recorded in TASK-013.
    This script imports `PoliteClient`/`RobotsPolicy` from `policy_scraper`
    directly. They stay where they are: the second-consumer rule that would move
    them into `packages/` counts features that ship behavior, and a dev seeding
    script is not one. Nothing is duplicated by importing them, so there is no
    drift for a package to prevent.
  - **Test (always on):** URL-list shape, payer slug resolution, request
    construction and the response handling for each of ingest's three dedup
    outcomes, against recorded fixture PDFs. These carry the 80% coverage gate.
    Tests live in `services/track-b-rag/tests/`, following the precedent that
    `scripts/seed-test-encounters.py` is tested from
    `services/track-a-clinical/tests/integration/`.
  - **Test (gated on an env flag, default off):** fetch the real Aetna and BCBS
    PDFs and assert each URL still resolves to a PDF. Add the job to
    `.github/workflows/nightly-live-checks.yml` alongside the CMS checks, naming
    the payer in the job name. A gate with no scheduled run is a deleted test —
    see CLAUDE.md's section on this.
  - **Which BCBS slug this seeds under was verified against real data, not
    chosen.** `Coverage.payor.display` values pulled from the Oracle Health
    (Cerner) open sandbox, the public HAPI R4 server and Synthea's own payer
    roster confirm that real feeds emit both unqualified ("Blue Cross") and
    Anthem-branded ("Anthem Blue Cross Blue Shield") names. `packages/payer-vocab`
    now carries three distinct Blue slugs — `anthem-bcbs`, the per-licensee
    `bcbs-ma`, and the generic `blue-cross-blue-shield` — because Association
    licensees publish their own criteria and a merged slug would let one
    licensee's policy silently answer for another. **Seed each BCBS document
    under the slug of the licensee that published it**, never the generic
    bucket. Re-ingest the local dev corpus after any further vocabulary change,
    per TASK-016.
  - Built (88 tests in `services/track-b-rag/tests/integration/test_seed_policies.py`
    — 83 always-on, 5 gated; the gated checks pass against both payers live).
    The corpus is 13 documents: 4 BCBSMA PDFs and 9 Aetna CPBs, so both ingest
    content types are exercised from this caller. Every target code has a
    document: 72148/72149 (CPB 0236, BCBSMA 935), 73721/73718 (CPB 0171, BCBSMA
    933), 29881 (CPB 0673), J7321/J7325 and 20610 (CPB 0179), 64483/62323
    (CPB 0016), and the provider-administered dermatology biologics (CPB 0905
    Cosentyx, 1009 Skyrizi, 0912 Stelara). The nightly workflow gained an
    `Aetna and BCBSMA policy documents` job on the `RUN_PAYER_LIVE_TESTS` gate.
  - Decisions worth knowing before touching this:
    - **Aetna's CPB index needs a browser; the documents do not.** The listing
      is behind a terms-acceptance modal and renders from two chained dropdowns.
      Individual CPB URLs fetch directly with no acceptance, which is why the
      script itself needs no browser and no crawler — a human reads the index
      once and adds entries.
    - **Every URL is verified by fetching it, not by trusting the index.** The
      BCBSMA "400" entry was wrong on first writing because it was copied from
      the overview page, where the link is stale; the gated live test caught it
      on its first run. Each entry's comment names codes that were confirmed
      present in the extracted text.
    - **CPB 0016 covers epidural injections, not CPB 0934.** 0934 (Epidural
      Injection Technologies) carries 62323 but not 64483; 0016 (Back Pain -
      Invasive Procedures) carries both.
    - **Taltz and Dupixent are absent on purpose.** Both are self-administered
      and sit on the pharmacy benefit, so they live in Aetna's separate Pharmacy
      CPB index. Seeding them means going to that index, not widening the search
      in the medical one.
    - `services/policy-scraper` gained a `py.typed` marker so the script's import
      of `PoliteClient` type-checks. `track-a-clinical` and `track-b-rag` already
      carried one; policy-scraper had no cross-boundary consumer until now.

- [x] **TASK-015:** Da Vinci CRD tier — two-tier policy lookup
  - Service: `services/track-b-rag`
  - **Scope: CRD only. DTR is deferred to a later task.** Earlier drafts titled
    this "CRD/DTR client", which overpromised relative to what is built here.
    DTR needs a SMART on FHIR app surface that does not exist before Phase 5.
  - Background: CMS-0057-F mandates Medicare Advantage, Medicaid managed care,
    CHIP, and ACA marketplace payers expose standardized FHIR-based prior
    authorization APIs (Da Vinci CRD for "is auth required / what's needed,"
    DTR for the actual documentation questionnaire) by January 1, 2027. This
    does NOT cover commercial employer-sponsored plans, which remain the RAG
    pipeline's primary job — CRD is an enhancement for the payers it applies
    to, not a replacement for TASK-010 through TASK-014.
  - Testable now, not blocked on 2027: the HL7 CRD Reference Implementation
    (github.com/HL7-DaVinci/CRD) is a real, spec-conformant simulated payer
    server, added to `docker-compose.yml` alongside the HAPI FHIR server. HL7's
    own image bakes the CDS-Library rule sets in, so there is nothing to load
    separately. ONC's Inferno (inferno.healthit.gov, CRD and DTR test kits) is
    the federal conformance tester and is not yet run against — a later task.
  - `is_crd_supported(payer: str) -> bool` — a small config-driven lookup, a
    literal set of the **canonical slugs** from `packages/payer-vocab` whose
    plans the mandate covers. Never a raw payer name: the query route already
    normalises through `_resolve_payer()`, and TASK-016 deliberately kept
    `medicare-advantage` from collapsing into `cms-medicare` for exactly this
    routing decision.
  - **The two tiers answer different questions and both run.** CRD decides
    `requires_auth`; the RAG path supplies `auth_criteria` and the step therapy
    fields. This is not what earlier drafts of this task said — they said a CRD
    answer would let us "skip the RAG/Qdrant/Sonnet path entirely." That was
    written before anyone ran a CRD server and it is wrong; see the findings
    below. The response model is identical either way and callers (TASK-021,
    TASK-040) never branch on which tier answered.
  - On any CRD failure — timeout, transport error, non-2xx, malformed body — or
    for a payer outside the mandate, the RAG path answers alone, unchanged.
  - **CRD answers are not cached, and the reason is not staleness.** The `rag:`
    cache exists because a Qdrant search plus a Sonnet call is expensive and its
    answer is identical for every patient on that payer/plan/state/CPT; a day of
    staleness is an accepted trade there. CRD is the opposite kind of thing: its
    entire value is being a live, authoritative answer from the payer's own
    system at the moment of the order. Caching it for 24 hours throws away the
    one property that makes it worth building. The CRD determination is applied
    *after* the RAG result is written to Redis, so it structurally cannot reach
    the cache.
  - **`crd` added to `RulesSource`.** With no cache write, "the CRD tier
    answered" is not inferrable from cache state, so the tests assert it
    directly. The composed values are `crd+rag`, `crd+cache`, and bare `crd` for
    a CRD answer where the policy tier had nothing.
  - Housekeeping: the CRD RI image is pinned in `docker-compose.yml` (the single
    source of truth for backing service versions, and what Dependabot watches);
    `CRD_BASE_URL` and `CRD_TIMEOUT_SECONDS` are in `.env.example`. No new
    Redis key patterns and no route, status code or response-shape changes, so
    `docs/api/track-b-rag.yaml` is untouched and its drift test still passes.
  - **Test:** stand up the CRD Reference Implementation locally, query
    `/policies/query` for a payer marked `is_crd_supported=True`, verify the CRD
    tier decided `requires_auth`
  - **Test:** query for a payer marked `is_crd_supported=False`, verify the RAG
    path is used as before and CRD is not called at all (regression test — this
    task must not change existing behavior for unsupported payers)
  - **Test:** simulate a CRD timeout/error, verify the RAG answer stands and the
    caller gets a normal response, not an error
  - **Test:** a CRD-answered query writes only the policy-text answer to the
    `rag:` key, never the determination
  - Built (512 tests, 100% coverage). Decisions worth knowing before touching this:
    - **CRD does not carry documentation criteria, and that is the standard, not
      the RI.** The task was written assuming "if it returns documentation
      requirements, use those directly." It does not return them. A covered code
      returns a card saying "Documentation Required" plus a `coverage-information`
      extension carrying `doc-needed` and a canonical URL pointing at a DTR
      Questionnaire. Reading the IG's own `ext-coverage-information`
      StructureDefinition confirmed this is by design: its slices are `covered`,
      `pa-needed`, `doc-needed`, `doc-purpose`, `info-needed`, `questionnaire`,
      `reason`, `detail`, `billingCode` and trace fields — none of them criterion
      text. CRD answers *whether*; DTR answers *what*. Hence the two-tier split
      above. Fetching the Questionnaire anyway was rejected on inspection: its
      items are mostly `Last Name`, `NPI`, `Signature`, so mapping them into
      `auth_criteria` would have Stage 2 report a clinician's note as missing
      "Signature".
    - **Two dialects, and `crd.read_determination` reads both.** A conformant
      payer states the answer in the `pa-needed` slice. The Reference
      Implementation never emits that slice at all — it emits a `coverageInfo`
      slice that is not in the IG's slice list, and states the determination in
      the card's *type* (`source.topic.code == "prior-auth"`). A mapping written
      to the IG alone finds nothing in RI output; one written to RI output alone
      misses a real payer. `pa-needed` is checked first and wins.
    - **Silence is never "no authorization required."** An empty card list, a
      documentation-only card, an "unable to process" card, and a code the payer
      holds no rule for all return *no determination*, and RAG answers alone.
      This is the one assertion in the live test worth defending hardest: if a
      future RI change made an unknown code read as a negative determination,
      this service would start telling providers that unauthorized orders are
      clear.
    - **The CRD request carries no patient, and that costs real coverage.**
      Stage 1 holds no patient data by construction — a unit test asserts
      `resolve_policy_rules` has no `clinical_context` parameter — so the
      request is built from payer, plan type, state and code with a placeholder
      subject. CRD is specified as a *patient-specific* check, so a rule keyed on
      age (HCPCS E0424, Home Oxygen Therapy) answers "unable to process" and
      falls through to RAG. Fabricating a birth date to make it answer would
      produce a confident determination about a person who does not exist.
      Closing this is **TASK-059**, which needs TASK-052's real FHIR `Patient`
      and `Coverage` resources first — and which turns the request into a PHI
      disclosure to a third party, with the TLS, endpoint-verification and
      audit obligations that follow. That is a different kind of task from this
      one, which is why it is not an amendment to it.
    - **Against this RI, every CPT code in our corpus falls through to RAG.**
      Its rule library is HCPCS/DME — home oxygen, hospital beds, ambulance
      transport. CPT 27447 and 70551 get a card that decides nothing. That is
      the honest behaviour and it is what the live test asserts; it also means
      the RI validates the *mapping*, not our clinical coverage.
    - **Latency measured, not assumed**, because with no caching every supported
      query hits the payer live. Against the RI on a developer machine: ~0.5s
      steady state (0.45–0.65s over eight calls), ~3.0s on the first request
      while it compiles its CQL libraries. That is faster than the RAG path it
      runs beside, so no short-TTL cache is warranted. `CRD_TIMEOUT_SECONDS`
      defaults to 4.0 to clear the cold start.
    - The two tiers run concurrently via `asyncio.gather`, so the CRD call adds
      no wall-clock time to a query that was going to run RAG anyway. A test
      makes each tier block until the other has started, so a regression to
      sequential execution times out rather than passing quietly.
    - **A CRD answer over a RAG fallback is a real answer, not a fallback.**
      Where we hold no policy text but the payer answered, the result is
      `requires_auth` from CRD with an empty criteria list, and Stage 2 runs on
      it. The existing nudge wording for that case already says the criteria
      could not be found and asks for a manual check, which is exactly the
      situation — no new nudge branch was needed.
    - The RI's README documents its discovery endpoint as `/cds-services`. The
      running server answers 404 there and 200 on `/r4/cds-services`, because
      its controller prefixes every route with the FHIR release. The path in
      `crd.CDS_SERVICES_PATH` came from the server; a live test asserts it so a
      move back is caught.
    - Every fixture in `tests/fixtures/crd/` was captured from the running RI
      rather than hand-written, per the same rule TASK-011's real-PDF and
      TASK-013's real-CMS-export checks follow. The one exception is the
      `pa-needed` shape, built from the IG StructureDefinition because no
      implementation we can reach emits it; it is labelled as such in the test
      module. `tests/integration/test_crd_live.py` is gated on
      `RUN_CRD_LIVE_TESTS` and runs nightly, so the fixtures cannot silently
      drift away from the server they came from.
    - CI: no new package and no new path-filter entry — this is service code
      inside `track-b-rag`. The nightly workflow gained a `davinci-crd` job that
      starts the RI from `docker-compose.yml` and dumps its log on failure.

- [x] **TASK-016:** Create shared `packages/payer-vocab`
  - Prerequisite for TASK-013 and TASK-014; a retrofit of TASK-011 and TASK-012,
    both of which shipped before this problem was spotted.
  - Full reasoning is in CLAUDE.md, "Payer and jurisdiction identity — one
    canonical vocabulary". The short version: `payer` is matched by exact string
    equality in `policy_query_filter`'s Qdrant filter and interpolated raw into
    the `rag:{payer}:{plan_type}:{state}:{cpt_code}` cache key, and nothing
    normalises it on either side. At query time the string comes from a FHIR
    `Coverage` resource's free-text display — "Medicare Part B", "AETNA" — which
    never equals the "CMS" or "Aetna" an ingest wrote. Retrieval then returns
    nothing and the service reports "no policy found", which is indistinguishable
    from the payer genuinely having no policy on file.
  - `normalize_payer(raw: str) -> str` — deterministic slug (casefold, strip
    legal suffixes and punctuation, collapse whitespace, hyphenate) layered over
    a curated alias table for what slugging cannot reach: "Medicare", "Medicare
    Part A", "Medicare Part B", "Original Medicare" and "CMS" all resolve to
    `cms-medicare`.
  - `is_known_payer(slug: str) -> bool` — an unrecognised payer still gets a slug
    and still queries, because a payer we have never seen is not an error. The
    query path logs at WARNING when this returns False, so a name that failed to
    line up is visible in the operational trace instead of looking like an empty
    result.
  - `normalize_state(raw: str) -> str` — the jurisdiction half. CMS's state
    vocabulary carries territories, a four-character `CNMI`, and the sub-state
    codes `DN`/`QN`/`UN`, `NF`/`SF` and `EM`/`WM`. Map each to the USPS code of
    its parent state.
  - Retrofit both track-b-rag routes to normalise on the way in — ingest before
    it writes Qdrant and Postgres, query before it builds the filter and the
    cache key. Keep the payer's own spelling in the `insurance_policies` row for
    humans; slugs are for matching.
  - Re-ingest the local dev corpus after the change. Doing this now costs one
    re-run of a dev-only index; doing it after TASK-014 seeds a real corpus
    means re-ingesting that instead. Same argument as TASK-010's package rename.
  - Its own path-filter entry and CI job, per the packages rule in CLAUDE.md's CI
    section — a new package's CI wiring ships with the package.
  - **Test:** the alias table's known pairs, including every Medicare spelling.
  - **Test:** two spellings of one payer produce one slug, hence one cache key
    and one Qdrant filter value.
  - **Test:** an unknown payer normalises without raising and reports
    `is_known_payer() == False`.
  - **Test:** every CMS sub-state and territory code maps to a two-character USPS
    code, `CNMI` included.
  - Built (64 package tests at 100% coverage; track-b-rag holds at 360 tests and
    100% after the retrofit). Decisions worth knowing before touching this:
    - **Display name and slug are different fields, and no migration was
      needed.** `insurance_policies.payer` keeps the payer's own spelling; the
      Qdrant payload and the `rag:` cache key carry the slug. That split works
      only because nothing selects from `insurance_policies` by payer — dedup is
      by `policy_id` — which was checked rather than assumed.
    - Normalisation happens at the two boundaries and nowhere else:
      `PolicyMetadata.payer_slug` on the ingest side, `_resolve_payer()` in the
      query route. `retrieval`, `policy_rules`, `cache` and `query` never see a
      display name, so there is one place to look when a payer does not match.
    - A payer name with no alphanumerics — `"---"` — is a 422 from the request
      model rather than a 500 from inside `normalize_payer`. An *unfamiliar*
      payer is not an error and never becomes one; only an unusable one is.
    - The alias table is keyed on the slug, not the raw string, so every casing
      and punctuation variant resolves through a single row. Three self-tests
      hold that shape: every alias key is its own slug, every alias target is a
      known payer, and every target is a fixed point under `normalize_payer`.
    - **Medicare Advantage deliberately does not collapse into `cms-medicare`.**
      MA plans layer their own prior-authorization rules over the national ones
      and TASK-015 routes them down the Da Vinci CRD path instead; merging them
      would answer an MA query out of traditional Medicare's policy text.
    - `tests/integration/test_policy_query.py`'s `index_policy` fixture indexes
      under the slug now. It exists to do what ingestion does, and indexing a
      display name there would have every retrieval in that module exercising a
      filter production never uses.
    - Trap worth knowing in that same module: it gives each test its own payer so
      each writes its own `rag:` key. A cross-spelling test written with a
      hardcoded "CMS"/"Medicare Part B" pair passed once and then failed on the
      next run — the first run's cache entry survived, the second run was served
      from it, and Bedrock was never called. Derive spellings from the `payer`
      fixture; do not hardcode a payer in a test that reaches the cache.
    - CI: `payer-vocab` added to `all_packages` in ci.yml, so it gets its own
      test and mypy jobs and a change to it re-tests every service. No new
      environment variables.

---

## Phase 2 — Audio Pipeline

- [x] **TASK-020:** Audio ingestion WebSocket server
  - Prerequisite: TASK-006 (session lifecycle) — this task validates the JWT
    that TASK-006 mints, it does not mint or manage sessions itself
  - Service: `services/audio-ingestion`
  - `WebSocket /ws/audio/{session_id}` — accepts raw audio chunks from client
  - Validate the session JWT against `JWT_SIGNING_KEY` before accepting the
    connection: verify signature, verify `exp` not passed, verify the token's
    `session_id` claim matches the URL's `session_id`. Close with code 4401 on
    any failure — do not accept the connection first and validate after.
  - The token arrives by *either* the `Authorization: Bearer` header *or* the
    `Sec-WebSocket-Protocol` subprotocol carrier, either one sufficient — see
    CLAUDE.md, "How the JWT reaches a WebSocket endpoint". An earlier draft of
    this task named the header alone; that predated anyone checking what a
    browser can send, and `apps/web` cannot set a header on the native
    `WebSocket` constructor at all. The mechanism is documented centrally rather
    than here because TASK-041 and TASK-023 both depend on it.
  - Buffer chunks in in-memory BytesIO — never write to disk
  - Forward stream to AWS Transcribe Medical streaming API
  - On transcript segment received: publish to Redis channel
    `transcription:{session_id}` per CLAUDE.md's Redis key list
  - On disconnect: explicitly clear BytesIO buffer, close Transcribe stream
  - **Test:** send 10 seconds of test audio WAV chunks with a valid JWT, verify
    transcript events in Redis
  - **Test:** connect with an expired or malformed JWT, verify connection closes
    with 4401 and no Transcribe stream is opened
  - Built (112 tests, 100% coverage). Decisions worth knowing before touching this:
    - **`amazon-transcribe` cannot do Transcribe Medical, and this service
      patches it so it can.** The official AWS streaming SDK, pinned at 0.6.4,
      implements `StartStreamTranscription` only: there is no
      `start_medical_stream_transcription`, no `specialty` or `type` anywhere in
      the package, and its serializer writes `/stream-transcription` as a
      literal. `boto3` is not a fallback — it has no streaming transcription API
      at all. So `src/transcribe_medical.py` subclasses the SDK's serializer and
      client to reach the medical operation, which differs only by a request URI
      and two headers. The response side needed nothing: the medical stream
      emits the same `:event-type: TranscriptEvent` framing and the SDK's parser
      reads every field with a tolerant `.get()`.
      This is a patch of internals, not configuration, so: the dependency is
      pinned `==0.6.4` rather than given a floor, and
      `tests/unit/test_transcribe_medical.py` asserts the *serialized request*
      rather than that the subclass exists. The failure mode being guarded
      against is silent — an override that stopped taking effect would still
      transcribe, just with the general model, quietly losing the clinical
      vocabulary TASK-021 and TASK-030 depend on. Treat a Dependabot bump here
      as a change to review.
    - **The JWT arrives by either carrier**, header or `Sec-WebSocket-Protocol`
      — see CLAUDE.md, "How the JWT reaches a WebSocket endpoint", which is
      where the mechanism is defined rather than here, because TASK-041 and
      TASK-023 both need it.
    - **4401 cannot reach a browser, and that is the right trade.** Validation
      happens before the handshake is accepted, so there is no WebSocket frame
      to carry a close code in and a real server answers the upgrade with an
      HTTP status instead. The 4401 is what the application emits and what an
      ASGI-level test observes. Accepting an unauthenticated handshake purely so
      the rejection reads nicely would be worse.
    - **Only stabilized segments are published.** Transcribe revises a partial
      result repeatedly under one `result_id` before finalising it. Forwarding
      those would multiply bus traffic and make TASK-021 fire the same procedure
      keyword over and over as one sentence is re-transcribed — one order
      becoming a stream of duplicate nudges. `is_partial` is in the payload, so
      a later task wanting earlier signal changes the publisher and not the
      message shape.
    - One audit row per accepted connection, not per segment: a ten-minute
      encounter is one act of access by one provider. A refused connection
      writes none — no PHI was reached — and logs at WARNING instead. The write
      goes on hipaa-logger's own pool rather than joining a transaction, because
      this service owns no tables and holds no database session.
    - `GET /health` reports Redis only. Transcribe is deliberately not probed:
      opening a streaming transcription per probe interval would bill for it and
      would report on a credential path rather than on this process's readiness.
    - **The integration suite fakes transcription too, and this is a real gap.**
      Transcribe Medical has no local emulator and moto does not implement the
      HTTP/2 event-stream protocol its streaming API uses — moto's `transcribe`
      support is batch jobs only. A live test would need real AWS credentials,
      which CI deliberately holds none of. So nothing in this repository proves
      an actual medical stream is accepted by AWS; the serialized-request
      assertions are the closest guard. This is *not* an env-gated test, because
      a gate with no scheduled run that opens it is a deletion — if the live
      check is ever wanted, it needs credentials and a
      `nightly-live-checks.yml` job in the same change.
    - Assertions in the integration suite happen while the socket is still open.
      The synchronous `TestClient` cancels the application task as soon as the
      WebSocket context exits, so anything the service does after the disconnect
      — including opening its first real Redis connection — loses that race.
    - The service keeps its bare `src/` package rather than being renamed the
      way TASK-010 renamed track-b-rag. Nothing imports it; it communicates by
      publishing to Redis. The task that first needs to import from here is the
      task that should rename it.
    - New environment variables: `TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ` and
      `TRANSCRIBE_MEDICAL_MEDIA_ENCODING`, both in `.env.example`. They must
      match what TASK-022 and TASK-023 capture — Transcribe answers a sample
      rate that disagrees with the audio by hanging, not by erroring.
    - CI needed no change: `audio-ingestion` was already in `all_services`, and
      the suite needs only the Redis that `docker compose` already starts.

- [x] **TASK-021:** Transcription event fan-out
  - Prerequisite: TASK-020 (publishes the events this task consumes), TASK-006
    (session lifecycle — the start signal this consumer subscribes on, and the
    end signal it releases on)
  - This is two separate consumers in two separate services, not one shared
    fan-out component — each subscribes to the same Redis channel independently:
    - Track A consumer lives in `services/track-a-clinical` (implemented as
      part of TASK-030) — accumulates full transcript per session_id
    - Track B consumer lives in `services/track-b-rag` — scans for procedure
      order keywords, implemented in this task
  - Subscribe to `transcription:{session_id}` per CLAUDE.md's Redis key list,
    **per session and never by pattern**. `transcription:*` would put a wildcard
    across the one channel family carrying speech and hand every consumer every
    encounter. Subscribe when `sessions:started` announces a session,
    unsubscribe on `session:ended:{session_id}` — the same shape as TASK-041's
    subscribe-on-connect/unsubscribe-on-disconnect, applied to a Redis consumer.
  - Keyword detection list: MRI, CT scan, X-ray, biopsy, injection, arthroscopy,
    echocardiogram, stress test, biologic, chemotherapy, referral to [specialist]
  - On keyword detected: extract surrounding context (the sentence or two
    containing the keyword) and call TASK-012's `/policies/query` **over HTTP**,
    with that context as `clinical_context` — not by importing
    `answer_policy_query()`, even though the route lives in this same service.
    The `audit_log()` write is in the route layer, and it records that a
    PHI-carrying request was made for a given provider and session; an
    in-process call would skip it, and moving the audit into the shared function
    so both paths were covered would put one compliance obligation in two
    places. One call path, one audit site.
  - **Suppress repeat mentions for the life of the encounter.** A procedure
    named three times in a visit is one order and must raise one nudge. The
    guard is Redis-backed rather than in-process, so it holds across replicas.
  - **This task does not produce real policy queries end to end, by design.**
    `/policies/query` needs `payer`, `plan_type`, `state` and `cpt_code`, and
    nothing in the system can supply them yet — see **TASK-024**, which is the
    task that closes it. So this task calls a seam,
    `policy_dispatch.resolve_and_query_policy()`, whose parameter-resolution
    half raises and whose HTTP half is real and tested. Placeholder values were
    considered and rejected: the cache key is
    `rag:{payer}:{plan_type}:{state}:{cpt_code}`, so a fabricated CPT code would
    file a real policy answer under a key standing for a different procedure and
    serve it to the next encounter. Same "build behind a seam and be honest
    about what is stubbed" pattern as TASK-012's Stage 1 seam and the CRD split.
  - **Test:** publish transcript with "let's order an MRI" — verify policy
    query fires with the correct extracted context
  - Built (539 unit tests + 5 integration, 100% coverage). Decisions worth
    knowing before touching this:
    - **TASK-006 gained a publish.** `POST /sessions/start` now announces the
      new `session_id` on the fixed `sessions:started` channel, because a
      per-session subscriber cannot name a channel for a session it has never
      heard of, and the only alternative was the wildcard this task rules out.
      The publish precedes the response, and a client cannot open the audio
      socket without the JWT in that response, so a consumer is always listening
      before the first segment can exist. A failed publish is a 503: an
      encounter nobody watches raises no nudges and is indistinguishable from an
      encounter with nothing to flag.
    - **`transcript_consumer` is now a `GET /health` flag** on track-b-rag, and
      it is the only one that is not a network dependency. It earns the place
      for the reason above — a stopped consumer silently disables nudges for
      every encounter on that instance, and nothing else in the service notices.
    - **The dedup key is the canonical keyword today, not the CPT code.** The
      guard is `SADD procedure_seen:{session_id}`, which reports first-add
      atomically in one round trip; a read-then-write pair would race, and
      Transcribe delivers stabilized results in bursts. `claim_procedure()`
      takes an opaque `procedure_key` so TASK-024 changes the call site and not
      the guard. Worth knowing: until it does, two keywords that map to one CPT
      code hold separate claims, because nothing yet knows they are the same
      procedure.
    - **A structural failure keeps its claim; a transient one gives it back.**
      `MissingQueryParameters` means every later mention would fail identically,
      so the claim stands and the TASK-024 warning is logged once per procedure
      per session rather than once per segment. A timeout or transport error
      releases the claim, so a later mention gets another attempt instead of
      being silently suppressed for the rest of the visit.
    - **`clinical_context` carries the excerpt and nothing else.** Stage 2's
      matcher flattens every value in that mapping into a term vocabulary and
      keeps digits of any length, deliberately, because a criterion's numbers
      usually *are* the criterion. Putting the segment's `start_time` or
      `result_id` in would feed stray digits into it and let a criterion reading
      "6 weeks" match a timestamp.
    - The excerpt cannot cross segment boundaries: this service holds no
      transcript, and a keyword in a segment's opening words carries only what
      preceded it in that segment. Buffering a rolling transcript here would
      mean two services holding the same PHI in memory for the whole encounter,
      and TASK-030 already holds one.
    - Partial results are dropped again here even though TASK-020 already drops
      them. The publisher's docstring is the contract; this is the belt to it,
      and it costs one dictionary lookup.
    - **Known gap: a restart loses the sessions in flight.** The watch set is in
      process, so a redeploy or a dropped Redis connection mid-encounter leaves
      those visits unwatched until they end. The consumer logs at WARNING naming
      the count. Rebuilding it would mean querying `encounters` for active rows
      on reconnect — real work with its own failure modes, and it belongs with
      the task that makes this path produce actual queries. TASK-030 makes the
      same trade for its transcript buffer.
    - The service keeps its `src/track_b_rag/` package and needs no rename; it
      was renamed in TASK-010. `audio-ingestion` still publishes into a bare
      `src/`, and nothing here imports it.
    - New environment variables: `POLICY_QUERY_BASE_URL` and
      `POLICY_QUERY_TIMEOUT_SECONDS`, both in `.env.example`. The first is
      track-b-rag's own address, since the caller and the route are the same
      process.
    - CI needed no change: both services were already in `all_services`, and the
      new integration suite needs only the Redis `docker compose` already
      starts.

- [ ] **TASK-022:** Mobile audio capture (React Native)
  - Prerequisite: TASK-020 (the WebSocket server this streams to — its wire
    contract is fixed and this task conforms to it), TASK-006 (mints the session
    JWT this task presents; this task neither mints nor refreshes it)
  - App: `apps/mobile` — **currently an empty `.gitkeep`.** This task scaffolds
    the Expo app before it can add a hook: `package.json`, Expo config,
    TypeScript in strict mode, Jest + React Native Testing Library, ESLint. The
    npm workspace entry already exists in the root `package.json`, and
    `ci.yml`'s `mobile` job already exists but no-ops on
    `if [ ! -f apps/mobile/package.json ]` — so the moment that file lands, CI
    starts running `lint`, `typecheck`, `test` and each has to exist and pass.
  - **Pin `jest-expo` at `^57.0.4`, not `57.0.0`.** `jest-expo@57.0.0` shipped
    with a peer dependency on `@react-native/jest-preset@^0.85.0` while SDK 57
    is on React Native 0.86, so `npm install` fails with ERESOLVE
    (expo/expo#47435, fixed in `57.0.1` and current at `^0.86.2` in `57.0.4`).
    This matters more here than in a standalone app: CI runs `npm ci` at the
    workspace root, so the conflict would fail the whole install and take the
    `web` and `fhir-types` jobs down with it, not just `mobile`. No `overrides`
    entry is needed on a current pin — do not copy one from a blog post.
  - **Capture uses `expo-audio`'s `useAudioStream`, not `expo-av`.** An earlier
    draft of this task said expo-av; it was written without checking, the same
    way the Node.js `fhir-integration` line was, and it is wrong twice over.
    expo-av records to a *file URI* and exposes no PCM callback, so it can
    satisfy neither the "audio never persists" constraint nor the 16kHz-PCM one;
    and it is deprecated, unpatched, and removed from Expo entirely as of SDK
    55. `expo-audio` is first-party, bundled in Expo Go, and its
    `useAudioStream` hook delivers real-time PCM to an `onBuffer` callback as an
    `ArrayBuffer` — in memory, never a file. Third-party native modules
    (`@siteed/expo-audio-studio`, `react-native-live-audio-stream`) were the
    fallback if it had not: they would force a development build and take on a
    separately-maintained native dependency. They are not needed.
  - `useAudioStream({ sampleRate: 16000, channels: 1, encoding: 'int16' })` —
    16kHz mono 16-bit PCM, matching `TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ` and
    `TRANSCRIBE_MEDICAL_MEDIA_ENCODING=pcm` in `.env.example`.
  - **`sampleRate` is a request, not a guarantee, and a silent mismatch hangs.**
    Expo's docs say the actual rate may differ if the hardware cannot deliver
    it, and each `AudioStreamBuffer` reports the rate it was actually captured
    at. TASK-020 already records that Transcribe answers a disagreeing sample
    rate by *hanging rather than erroring*. A vague "fail loudly" would
    reproduce that same failure one layer up, so the behaviour is specified
    exactly:
    - **The check runs before the socket is opened, not after.** The actual
      rate is unknowable until the first buffer arrives, so the order is:
      request permission → start the stream → inspect the first
      `AudioStreamBuffer` → compare its `sampleRate` and `channels` against
      what was requested → **only then** open the WebSocket. Until that
      comparison passes, no socket exists and not one byte of audio has left
      the device.
    - **A mismatch is terminal for the attempt, and silent about nothing.**
      The hook stops the stream, discards the buffer, never opens the socket,
      and settles in an `error` state carrying a typed
      `SAMPLE_RATE_UNSUPPORTED` code with the requested and actual values. It
      does not retry, and it does not fall back to streaming at the wrong
      rate — the whole point is that the wrong rate looks like success until
      it hangs.
    - **It is returned, never thrown.** The hook's state is a discriminated
      union (`idle` | `requesting-permission` | `starting` | `streaming` |
      `error`), per the "errors bubble up as typed Result objects, not thrown
      exceptions" rule in CLAUDE.md's TypeScript conventions. That rule is
      load-bearing here rather than stylistic: a thrown error is exactly what
      a React error boundary would swallow, which is the failure mode this
      bullet exists to prevent.
    - **Surfacing it to a person is TASK-025's job, and TASK-025 does not
      exist yet.** This hook cannot block a "start visit" button that has not
      been built — `apps/mobile` has no session UI task, only this one and
      TASK-043. What this task can do is guarantee the error is *available*
      and unmissable in the hook's contract; the requirement that a screen
      refuse to start the visit and show it is written into TASK-025 below.
      Until then, a caller that ignores the `error` state gets no audio and no
      socket, which fails closed.
    - Resampling in JS is out of scope here; if a real device turns out to
      need it, that is its own task.
  - **The 250ms chunking happens in this hook.** `useAudioStream` exposes no
    buffer-size or interval control, so `onBuffer` delivers whatever size the
    native layer chooses. Accumulate into an in-memory byte buffer and flush at
    the 250ms boundary — 8000 bytes at 16kHz mono int16 — which is the same
    re-chunking shape `AudioBuffer.take_chunks()` does on the server. Assert
    little-endian int16 explicitly; Transcribe requires it, and both target
    platforms happen to be little-endian, which is exactly the kind of thing
    that works until it does not.
  - Streams to `ws://<host>:8001/ws/audio/{session_id}` as **binary frames**.
    A text frame gets the connection closed with 1003 by TASK-020's server.
  - **Mobile may use the `Authorization: Bearer` header carrier.** React
    Native's `WebSocket` accepts a headers option, unlike the browser's native
    constructor that forces TASK-023 into the subprotocol form. Either carrier
    is accepted and validated identically — see CLAUDE.md, "How the JWT reaches
    a WebSocket endpoint". The token is never logged and never put in the URL.
  - A rejected token fails the upgrade rather than surfacing as an `onclose`
    with 4401, for the reason that same section gives, so treat a connection
    that never opens as an auth failure and re-mint the session before retrying.
    The JWT's lifetime is `SESSION_TTL_SECONDS` (15 min), so a long encounter
    will outlive it.
  - Handles the microphone permission request via
    `requestRecordingPermissionsAsync()`, and surfaces denial as a state the UI
    can act on rather than throwing.
  - Stops the stream, closes the socket, and drops the pending buffer on session
    end and on unmount. Nothing is written to disk on any path.
  - **Test:** unit test the hook with mocked `expo-audio` — feed `onBuffer`
    synthetic buffers and assert exactly 8000-byte binary frames are sent, that
    a partial tail is not sent early, that a `buffer.sampleRate` disagreeing
    with the request fails loudly instead of streaming, that permission denial
    is surfaced, and that stop clears the buffer.

- [ ] **TASK-023:** Browser audio capture (React Web)
  - Prerequisite: TASK-020 (the WebSocket server this streams to — its wire
    contract is fixed and this task conforms to it), TASK-006 (mints the session
    JWT this task presents; this task neither mints nor refreshes it)
  - App: `apps/web`
  - `useAudioCapture` hook using MediaRecorder API
  - Same 16kHz mono, 250ms chunks, WebSocket stream
  - **The session JWT goes in the subprotocol list, not a header.** The native
    `WebSocket` constructor accepts a URL and subprotocols and nothing else, so
    open the socket as `new WebSocket(url, ["medauth.session.v1",
    `medauth.jwt.${jwt}`])`. TASK-020's server accepts either carrier and echoes
    `medauth.session.v1` back; see CLAUDE.md, "How the JWT reaches a WebSocket
    endpoint". Do not rediscover this by finding that headers are unavailable.
  - A rejected token fails the upgrade rather than surfacing as an `onclose`
    with code 4401 — the same section says why — so treat a connection that
    never opens as an auth failure and re-mint the session before retrying.
  - **Test:** jsdom mock of MediaRecorder

- [ ] **TASK-024:** Policy query parameters — encounter state and procedure codes
  - Prerequisite: TASK-021 (defines the seam this task fills in), TASK-005
    (owns the `encounters` migration this task adds a column to)
  - **What this closes.** TASK-021 detects a procedure in a live transcript and
    can name it, but `POST /policies/query` also needs `payer`, `plan_type`,
    `state` and `cpt_code`, and nothing supplies them. The gap was found while
    building TASK-021 and deliberately left open there rather than papered over:
    `policy_dispatch.resolve_query_parameters()` raises
    `MissingQueryParameters` on every call, the consumer logs it once per
    procedure per session, and this task replaces that function body. Nothing
    else in the consumer changes — that is what the seam is for.
  - **Nothing here may be approximated.** The Redis cache key is
    `rag:{payer}:{plan_type}:{state}:{cpt_code}`. A guessed CPT code does not
    merely return a poor answer for one encounter: it writes a real, cacheable
    policy answer under a key that stands for a different procedure, and the
    next encounter matching that key is served it. The failure is silent and
    crosses patients, which is why this is its own reviewed task.
  - **Add `state` to `encounters`** via a real Alembic migration in
    track-a-clinical (`alembic_version_track_a_clinical`, per CLAUDE.md).
    Two-character USPS code, nullable — it is unknown until an EHR launch
    supplies it. Note it is *not* the payer's jurisdiction: TASK-013 already
    normalises CMS's sub-state codes (`DN`/`QN`/`UN`, `NF`/`SF`, `EM`/`WM`,
    `CNMI`) to a parent state on the ingestion side, and this column is the
    other half of the comparison, so it must use the same vocabulary.
  - **Design the keyword-to-CPT mapping.** This is the part with real design
    content and should not be reduced to a dictionary literal without answering:
    - Which code, when a keyword covers many? "MRI" alone spans dozens of CPT
      codes by body part and contrast. The transcript excerpt names the body
      part often enough to matter, and the excerpt is already extracted.
    - What happens when the mapping is ambiguous or absent? A wrong code is
      worse than no query, per the cache reasoning above — so the honest answer
      is likely to be "do not query, and say so", the same shape TASK-021 uses
      now.
    - How does a new specialty extend it? MedAuth targets orthopedics and
      dermatology first (see the EHR priority order); a hardcoded dict that a
      dermatologist cannot extend without a deploy is a product problem, not
      only a code one.
    - Where does it live? Both track-b-rag and, later, prior-auth need procedure
      codes. If a second consumer appears, it belongs in `packages/`, on the
      same reasoning as `packages/payer-vocab`.
  - **Populate `payer`, `plan_type` and `state` at SMART launch.** The payer
    columns already exist on `encounters` and are filled from a FHIR `Coverage`
    resource — that is TASK-051/TASK-052's work, so this task depends on them
    for a *real* value and should not invent one meanwhile. Resolve the payer
    through `normalize_payer()` from `packages/payer-vocab`, never a raw
    display name.
  - **Move TASK-021's dedup key to the CPT code** once the mapping exists.
    `claim_procedure()` already takes an opaque `procedure_key`; today the
    caller passes the canonical keyword, so two keywords naming one code hold
    separate claims and can raise two nudges for one order.
  - Consider, and decide deliberately: whether the consumer reading `encounters`
    to build a query is itself a PHI access needing its own `audit_log()` row,
    or whether the row `/policies/query` already writes covers it. Reading only
    the non-patient columns is the reason to say no; Known Constraints #6 says
    to flag rather than guess, and this is the flag.
  - **Test:** a transcript naming "MRI of the left knee" produces a query with a
    knee MRI CPT code, not a generic one
  - **Test:** a keyword with no confident code mapping produces no query and a
    log line naming the keyword — never a query with a placeholder code
  - **Test:** the migration adds `state` and the model round-trips it
  - **Test:** end to end over Redis, replacing TASK-021's stubbed seam — a
    published transcript segment produces a real `/policies/query` call

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
    (verify signature, exp, session_id claim match; 4401 on failure), and the
    same two token carriers: `Authorization: Bearer` or the
    `Sec-WebSocket-Protocol` entry. See CLAUDE.md, "How the JWT reaches a
    WebSocket endpoint" — `apps/web` opens this socket too and can only use the
    subprotocol form.
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

- [ ] **TASK-059:** Patient-specific Da Vinci CRD
  - Service: `services/track-b-rag` (with `services/fhir-integration` as the
    data source)
  - Prerequisite: **TASK-052** (`get_patient()` / `get_coverage()` — the FHIR
    `Patient` and `Coverage` resources this task needs do not exist before it)
    and TASK-015 (the CRD client, mapping and support table this extends).
  - **The limitation this closes.** TASK-015's CRD request carries no patient.
    Stage 1 holds none by construction — `resolve_policy_rules()` has no
    `clinical_context` parameter and `test_stage_one_cannot_see_a_patient`
    asserts that signature — so the request is built from payer, plan type,
    state and procedure code with a placeholder subject. But CRD is specified
    as a *patient-specific* coverage check: a payer rule keyed on age or sex
    cannot be evaluated from what Stage 1 is allowed to know. Those requests
    come back as an "unable to process" card and fall through to the RAG path.
    Every rule in the HL7 Reference Implementation's Home Oxygen Therapy topic
    (HCPCS E0424) behaves this way. The tier therefore answers for a fraction
    of the rules a real payer publishes.
  - **Where the call goes, and why it cannot stay where it is.** Not into
    Stage 1: its result is cached under the payer-scoped
    `rag:{payer}:{plan_type}:{state}:{cpt_code}` key, and patient data reaching
    a payer-scoped cache would serve patient B the answer computed for patient
    A — the failure CLAUDE.md's cache note exists to prevent. Lift the CRD call
    *out* of Stage 1 into its own tier running alongside Stages 1 and 2. This
    works precisely because TASK-015 already established that **CRD results are
    never cached**: an uncached tier may see the patient without creating the
    cache-poisoning problem at all. The structural constraint being violated
    today is only that the call currently lives inside Stage 1.
  - **This makes the request a PHI disclosure to a third party, and that is the
    bulk of the work.** TASK-015's request carries nothing about a patient, so
    it needed no disclosure controls. This one does. Prior authorization is a
    payment purpose and the disclosure is permitted — it is exactly what CRD was
    designed for — but permitted is not the same as unguarded:
    - **TLS becomes mandatory rather than a deployment convention.**
      `CRD_BASE_URL` currently defaults to `http://localhost:8006`, the local
      Reference Implementation over loopback, and TASK-015's pull request left
      the TLS checkbox deliberately unticked on the grounds that the request
      carries nothing. That reasoning expires here. Reject a non-`https://`
      endpoint outright for any request carrying a patient; a plaintext
      loopback container is still fine for the patient-free path.
    - **A per-payer verified endpoint registry replaces the single
      `CRD_BASE_URL`.** One environment variable naming "the" CRD server is
      adequate for a payer-policy question. Sending PHI to a misconfigured or
      unverified endpoint is a disclosure to the wrong party, so the endpoint a
      patient's data goes to must be bound to that patient's payer.
    - **The disclosure needs its own `audit_log()` row**, distinct from the
      access row `/policies/query` already writes. Sending PHI outside the
      cluster is a different event from reading it, and the audit trail has to
      be able to answer "what left, to whom" and not only "who looked".
    - **Minimum necessary applies.** Send the demographics the payer's rule
      needs, not the whole patient context.
  - **What this does not do: make CRD the primary path.** CMS-0057-F covers
    Medicare Advantage, Medicaid managed care, CHIP and ACA marketplace plans
    only. Commercial employer-sponsored plans — the bulk of what the target
    private practices see — are not mandated and will not have CRD endpoints.
    This raises the hit rate *within* mandated payers. RAG remains the engine,
    and `is_crd_supported()` still gates the whole tier.
  - **Test:** a rule the Reference Implementation cannot evaluate without
    demographics (HCPCS E0424, Home Oxygen Therapy) returns a determination
    once a real `Patient` is supplied, where TASK-015's patient-free request
    gets "unable to process". This is the regression that proves the task
    achieved anything.
  - **Test:** a patient-carrying request to a non-`https://` endpoint is
    refused before the request is built, and the query still gets an answer
    from the RAG path rather than an error.
  - **Test:** the disclosure writes an `audit_log()` row naming the payer and
    the session, and no clinical detail appears in it.
  - **Test:** the patient-specific result is never written to the `rag:` cache —
    the same assertion TASK-015 makes, re-stated here because the data now
    reaching the tier is what would make a cache write a patient-safety bug
    rather than merely a staleness one.
  - **Test:** a payer outside the mandate is not consulted at all, and no
    patient data is assembled for it (regression — the tier must not widen its
    own scope by acquiring a data source).

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
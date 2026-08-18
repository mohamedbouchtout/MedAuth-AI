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

- [ ] **TASK-006:** Session lifecycle endpoints
  - Service: `services/track-a-clinical` (owns the encounters table)
  - Full spec is in CLAUDE.md "Session Lifecycle & JWT Issuance" — read that first,
    this task did not exist in earlier drafts and several later tasks assume it:
    TASK-020, TASK-021, TASK-030, TASK-041, and TASK-060 all depend on this.
  - `POST /sessions/start` — body: `{patient_id, provider_id, ehr_encounter_id}`.
    Creates `encounters` row, mints a 15-min HS256 JWT with claims
    `{session_id, provider_id, exp}` signed with `JWT_SIGNING_KEY`, returns
    `{session_id, jwt}`. All accesses logged via hipaa-logger.
  - `POST /sessions/{session_id}/end` — sets status/ended_at, publishes
    `session:ended:{session_id}` to Redis (empty payload, signal only)
  - Add `JWT_SIGNING_KEY` to `.env.example`
  - **Test:** start a session, verify JWT decodes with correct claims and 15-min expiry
  - **Test:** end a session, verify Redis subscriber receives the signal

---

## Phase 1 — RAG Pipeline (Build This First)

The insurance policy RAG is the technical core. Build and validate before other services.

- [ ] **TASK-010:** Set up Qdrant collection + embedding model
  - Service: `services/track-b-rag`
  - Initialize Qdrant `insurance_policies` collection (cosine, 1024 dims) using the
    idempotent get-or-create pattern in CLAUDE.md "Qdrant Initialization — Must Be
    Idempotent." Do NOT use `recreate_collection()` in startup code — it deletes
    all indexed policies on every service restart, which is a real bug carried
    over from an earlier draft of this architecture and must not ship.
  - Load `BAAI/bge-large-en-v1.5` embedding model via sentence-transformers
  - `GET /health` returns `{"qdrant": "ok"|"error", "embedding_model": "ok"|"error"}`
    — 200 only if both are ok, 503 otherwise
  - **Test:** embed a query string, verify vector shape is (1024,)
  - **Test:** call ensure_collection() twice in a row against a populated collection,
    verify no data loss (this is the regression test for the recreate_collection bug)

- [ ] **TASK-011:** Policy ingestion pipeline
  - Service: `services/track-b-rag`
  - `POST /policies/ingest` accepts PDF file + metadata (payer, plan_type, state, policy_id)
    — internal service-to-service endpoint only (called by policy-scraper and by
    scripts/seed-policies.py), not exposed to any frontend app
  - PDF parsing via PyMuPDF (fitz) — handles multi-column medical policy docs
  - Chunking: RecursiveCharacterTextSplitter, chunk_size=800, overlap=150
  - `content_hash` = SHA-256 hex digest of the raw PDF bytes (not the extracted
    text — two PDFs with identical text but different formatting should still be
    treated as distinct source files for audit purposes)
  - Dedup behavior: if `policy_id` already exists in `insurance_policies` with a
    matching `content_hash`, skip re-ingestion and return 200 with `{"status": "unchanged"}`.
    If `policy_id` exists with a different hash, re-ingest (delete old Qdrant points
    for that policy_id first, then insert new ones) and return `{"status": "updated"}`.
    If `policy_id` is new, ingest and return `{"status": "created"}`.
  - Embed chunks + upsert to Qdrant with metadata payload
  - Store metadata record in `insurance_policies` table
  - **Test:** ingest a sample 10-page PDF, verify chunks appear in Qdrant
  - **Test:** ingest the same PDF twice, verify second call returns "unchanged" and
    does not duplicate Qdrant points
  - **Test:** ingest a modified version of an existing policy_id, verify old points
    are removed and "updated" is returned

- [ ] **TASK-012:** Policy query endpoint
  - Service: `services/track-b-rag`
  - `POST /policies/query` — Pydantic request model:
    `{procedure: str, cpt_code: str, payer: str, plan_type: str, state: str, clinical_context: dict}`
  - Response model: `{requires_auth: bool, auth_criteria: list[str], missing_criteria: list[str],
    denial_risk: Literal["low","medium","high"], nudge_message: str, step_therapy_required: bool,
    step_therapy_details: str | None}`
  - Cache key: `rag:{payer}:{plan_type}:{state}:{cpt_code}` per CLAUDE.md's Redis key list, TTL 24h
  - If cache miss: embed query, search Qdrant (top 8, payer+state filter), call Claude
    **Sonnet** via Bedrock (see CLAUDE.md "Bedrock Model Assignment" — this call site
    is Sonnet, not Haiku, because it's multi-step reasoning over retrieved text)
  - Claude is instructed to return only JSON. If the response fails to parse as valid
    JSON matching the response model: retry once with the same prompt. If the retry
    also fails, return a safe fallback: `{requires_auth: true, auth_criteria: [],
    missing_criteria: [], denial_risk: "high", nudge_message: "Unable to verify
    authorization requirements — confirm manually", step_therapy_required: false,
    step_therapy_details: null}` and log the parse failure (no PHI in that log line —
    log the payer/procedure/cpt_code, never clinical_context contents). Fail toward
    "flag for manual review," never toward "assume no auth needed."
  - Cache the result only on a real (non-fallback) response — don't cache the fallback
  - **Test:** query for "knee MRI" + "Aetna PPO MA" — verify structured JSON returned
  - **Test:** second identical query — verify Redis cache hit (no Bedrock call)
  - **Test:** mock Bedrock returning malformed JSON — verify retry, then fallback,
    verify fallback is not cached

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
    that context as `clinical_context`
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

6. **Every new API route needs:** (a) Pydantic request/response models, (b) hipaa-logger call, (c) OpenAPI docstring, (d) at least one integration test.

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
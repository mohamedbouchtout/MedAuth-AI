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
  - Root `package.json` with npm workspaces covering apps/web and apps/mobile only
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

- [ ] **TASK-002:** Create shared `packages/hipaa-logger`
  - Owns its own audit_log table — see CLAUDE.md "hipaa-logger — Design Decisions"
    for the full schema and the reasoning for this ownership split
  - Alembic migration in `packages/hipaa-logger/migrations/` creating the audit_log table
    (this migration must run before any service-level migration — see TASK-005 note)
  - Alembic `version_table = "alembic_version_hipaa_logger"` — never the default
    `alembic_version`, which two migration setups sharing one database would corrupt
  - Raw asyncpg (not SQLAlchemy) — self-managed lazy connection pool from DATABASE_URL,
    with an injection hook accepting an optional `conn` param for tests and for callers
    that want the audit write inside their own transaction
  - Defensive DSN normalization: strip a SQLAlchemy driver suffix (`postgresql+asyncpg://`
    → `postgresql://`) so one DATABASE_URL value works for every consumer
  - `async def audit_log(actor_id, action, resource_type, resource_id, session_id, service_name, request_id=None, ip_address=None, user_agent=None, conn=None)`
  - Unit tests with mocked asyncpg (mock the pool/connection, not a real DB)
  - Integration test against real local Postgres: run the migration, call audit_log(),
    verify the row lands with correct columns
  - Update `.github/workflows/ci.yml` so packages/ get their own path-filter entries
    and test jobs — before this, a packages/ change only triggered service jobs and
    package tests never ran at all
  - **Test:** open a PR touching only `packages/hipaa-logger/**` and confirm the
    hipaa-logger job runs

- [ ] **TASK-003:** Create shared `packages/crypto-utils`
  - AES-256-GCM encrypt/decrypt using a KEK from AWS KMS + per-record DEK
  - `encrypt_field(plaintext: str, context: dict) -> EncryptedField`
  - `decrypt_field(encrypted: EncryptedField, context: dict) -> str`
  - Unit tests with mocked KMS

- [ ] **TASK-004:** Create shared `packages/fhir-types`
  - Pydantic v2 models for FHIR R4 resources used by this project
  - Patient, Encounter, Condition, Coverage, DocumentReference, Claim
  - Mirror in TypeScript interfaces for the fhir-integration Node service

- [ ] **TASK-005:** Initialize database schema + migrations
  - Create Alembic setup in `services/track-a-clinical` (owns the domain schema)
  - `version_table = "alembic_version_track_a_clinical"` — see constraint 8 below
  - Tables: encounters, clinical_notes, clinical_nudges, prior_auth_requests,
    insurance_policies (see architecture doc for full schema)
  - Note: audit_log is NOT created here — it's owned by packages/hipaa-logger
    (TASK-002) and its migration must be applied first. Run hipaa-logger's
    migration before this service's migrations in any fresh environment.
  - `scripts/init-db.sh` runs migrations in order: hipaa-logger first, then
    track-a-clinical's domain tables
  - Seed script with 5 test encounters linked to Synthea patient IDs

---

## Phase 1 — RAG Pipeline (Build This First)

The insurance policy RAG is the technical core. Build and validate before other services.

- [ ] **TASK-010:** Set up Qdrant collection + embedding model
  - Service: `services/track-b-rag`
  - Initialize Qdrant `insurance_policies` collection (cosine, 1024 dims)
  - Load `BAAI/bge-large-en-v1.5` embedding model via sentence-transformers
  - Startup health check that verifies Qdrant connection + model loaded
  - **Test:** embed a query string, verify vector shape is (1024,)

- [ ] **TASK-011:** Policy ingestion pipeline
  - Service: `services/track-b-rag`
  - `POST /policies/ingest` accepts PDF file + metadata (payer, plan_type, state, policy_id)
  - PDF parsing via PyMuPDF (fitz) — handles multi-column medical policy docs
  - Chunking: RecursiveCharacterTextSplitter, chunk_size=800, overlap=150
  - Embed chunks + upsert to Qdrant with metadata payload
  - Store metadata record in `insurance_policies` table (content hash for dedup)
  - **Test:** ingest a sample 10-page PDF, verify chunks appear in Qdrant

- [ ] **TASK-012:** Policy query endpoint
  - Service: `services/track-b-rag`
  - `POST /policies/query` — accepts procedure, cpt_code, payer, plan_type, state, clinical_context
  - Check Redis cache first: key = `rag:{payer}:{plan_type}:{state}:{cpt_code}`, TTL 24h
  - If cache miss: embed query, search Qdrant (top 8, payer+state filter), call Claude Sonnet
  - Claude prompt: analyze policy chunks against clinical context, return structured JSON:
    `{requires_auth, auth_criteria, missing_criteria, denial_risk, nudge_message, step_therapy_required}`
  - Cache the result, return to caller
  - **Test:** query for "knee MRI" + "Aetna PPO MA" — verify structured JSON returned
  - **Test:** second identical query — verify Redis cache hit (no Bedrock call)

- [ ] **TASK-013:** Policy scraper (background CronJob)
  - Service: `services/policy-scraper`
  - Scrape CMS Medicare coverage database (NCD/LCD pages) — public, no auth required
  - Download PDFs, hash content, skip if hash matches existing record
  - Trigger ingestion pipeline for new/changed policies
  - Kubernetes CronJob manifest: runs nightly at 2am UTC
  - **Test:** run against CMS sandbox, verify at least one policy is downloaded + hashed

- [ ] **TASK-014:** Seed Qdrant with real payer policies for dev
  - `scripts/seed-policies.py`
  - Download publicly available policy PDFs from: CMS (Medicare), Aetna, BCBS (public guidelines)
  - Ingest into local Qdrant via TASK-011 endpoint
  - Focus on: orthopedic MRI (CPT 72148), knee arthroscopy (CPT 29881), biologic injections

---

## Phase 2 — Audio Pipeline

- [ ] **TASK-020:** Audio ingestion WebSocket server
  - Service: `services/audio-ingestion`
  - `WebSocket /ws/audio/{session_id}` — accepts raw audio chunks from client
  - Validate session JWT before accepting connection (401 close if invalid)
  - Buffer chunks in in-memory BytesIO — never write to disk
  - Forward stream to AWS Transcribe Medical streaming API
  - On transcript segment received: publish to Redis channel `transcription:{session_id}`
  - On disconnect: explicitly clear BytesIO buffer, close Transcribe stream
  - **Test:** send 10 seconds of test audio WAV chunks, verify transcript events in Redis

- [ ] **TASK-021:** Transcription event fan-out
  - When transcript segment published to `transcription:{session_id}`:
    - Track A consumer (in track-a-clinical) accumulates full transcript
    - Track B consumer (in track-b-rag) scans for procedure order keywords
  - Keyword detection list: MRI, CT scan, X-ray, biopsy, injection, arthroscopy,
    echocardiogram, stress test, biologic, chemotherapy, referral to [specialist]
  - On keyword detected: extract context, call TASK-012 policy query endpoint
  - **Test:** publish transcript with "let's order an MRI" — verify policy query fires

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
  - Service: `services/track-a-clinical`
  - Subscribe to Redis `transcription:{session_id}`
  - Accumulate segments into rolling transcript buffer per session
  - On session end event: call Claude Sonnet via Bedrock with full transcript
  - Prompt: generate SOAP note + extract ICD-10 codes + anticipated CPT codes
  - Store result in `clinical_notes` table
  - **Test:** send sample orthopedic encounter transcript, verify SOAP structure returned

- [ ] **TASK-031:** Comprehend Medical validation layer
  - After LLM extracts ICD-10 codes, validate with AWS Comprehend Medical InferICD10CM
  - Flag any codes LLM returned that Comprehend Medical did not confirm (confidence < 0.8)
  - Log discrepancies for quality monitoring
  - **Test:** run on transcript with 3 clear diagnoses, verify codes match

- [ ] **TASK-032:** SOAP note review endpoint
  - `GET /notes/{encounter_id}` — return generated SOAP note for provider review
  - `PATCH /notes/{encounter_id}` — provider edits, sets `provider_edited = true`
  - All accesses logged via hipaa-logger

---

## Phase 4 — Live Nudge System

- [ ] **TASK-040:** Nudge emitter
  - Service: `services/track-b-rag`
  - When policy query returns `missing_criteria` or `denial_risk = high`:
    publish nudge to Redis `nudges:{session_id}`
  - Nudge payload: `{type, procedure, cpt_code, message, missing_criteria, denial_risk, haptic}`
  - Store nudge in `clinical_nudges` table
  - **Test:** trigger policy query with known missing criteria, verify Redis pub

- [ ] **TASK-041:** Nudge WebSocket relay
  - Service: `services/nudge-service`
  - `WebSocket /ws/nudges/{session_id}`
  - Subscribe to Redis `nudges:{session_id}`
  - Forward each nudge event to connected client in real time
  - On client disconnect: unsubscribe from Redis channel
  - **Test:** publish nudge to Redis, verify it appears at WebSocket client

- [ ] **TASK-042:** Nudge UI component (web)
  - App: `apps/web`
  - `<NudgeOverlay sessionId={...} />` — subscribes to nudge WebSocket
  - High-contrast banner, color-coded by denial_risk (yellow/orange/red)
  - Dismiss button — marks nudge acknowledged via API call
  - Accessible (ARIA role=alert, focus management)
  - **Test:** render with mock WebSocket, verify alert appears on message

- [ ] **TASK-043:** Haptic nudge (mobile)
  - App: `apps/mobile`
  - On nudge received with `haptic: true`: call `Haptics.notificationAsync()`
  - Same visual alert as web
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
  - Service: `services/prior-auth`
  - Triggered on session end (subscribe to `session:ended:{session_id}`)
  - Fetches: clinical_note (Track A), nudges fired (Track B), patient FHIR data
  - Assembles `prior_auth_bundle`: patient, provider, procedures, diagnoses, clinical evidence
  - Stores in `prior_auth_requests` table with status = 'pending'
  - **Test:** assemble bundle from test encounter data, verify all fields populated

- [ ] **TASK-061:** Submission router
  - Service: `services/prior-auth`
  - `POST /prior-auth/{request_id}/submit`
  - Checks payer capabilities: supports FHIR PAS? → use TASK-053. Otherwise → CoverMyMeds API
  - Updates `prior_auth_requests.status` and `submission_method`
  - **Test:** mock both submission paths, verify correct one chosen per payer config

---

## Phase 7 — Provider Dashboard (Web App)

- [ ] **TASK-070:** Session management UI
  - Start session (select patient from FHIR search, begin recording)
  - Active session view (live transcript, nudge overlay, clinical checklist)
  - End session (triggers SOAP generation + bundle assembly)

- [ ] **TASK-071:** Note review + edit UI
  - Display generated SOAP note in editable form
  - Provider can edit any section before EHR write-back
  - One-click write-back to EHR
  - Show ICD-10 and CPT codes extracted, allow corrections

- [ ] **TASK-072:** Prior auth status dashboard
  - List of all prior auth requests with status (pending, submitted, approved, denied)
  - Denial reason display
  - Resubmission flow for denied requests

---

## Known Constraints for Claude Code

1. **Do not install Kafka.** Redis pub/sub is the message bus. Any task that says "publish event" means `redis.publish()`.

2. **AWS Bedrock, not direct Anthropic API.** All LLM calls use `langchain_aws.ChatBedrock` with `boto3.client('bedrock-runtime')`. Never import `anthropic` directly.

3. **Moto for all AWS mocking in tests.** `@mock_aws` decorator on test functions that call Bedrock, Transcribe Medical, Comprehend Medical, KMS.

4. **Pydantic v2 syntax.** `model_config = ConfigDict(...)` not `class Config:`. `model_validate()` not `parse_obj()`.

5. **SQLAlchemy 2.0 async style.** `async with async_session() as session:` pattern. No sync session calls.

6. **Every new API route needs:** (a) Pydantic request/response models, (b) hipaa-logger call, (c) OpenAPI docstring, (d) at least one integration test.

7. **TASK numbers must be recorded in commit messages.** Format: `feat(track-b-rag): implement policy query endpoint [TASK-012]`

8. **Namespace every Alembic version table.** Each migration setup sets
   `version_table = "alembic_version_{name}"` in its `env.py` — `alembic_version_hipaa_logger`
   for packages/hipaa-logger, `alembic_version_track_a_clinical` for track-a-clinical, and the
   same pattern for every future service. Multiple setups share one database; on the default
   `alembic_version` table each would read another's revision as its own head.

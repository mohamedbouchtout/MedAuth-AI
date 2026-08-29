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

- [x] **TASK-006b:** Re-mint a session token without starting a session
  - Prerequisite: TASK-006 (owns session lifecycle and the `encounters` table)
  - Service: `services/track-a-clinical`
  - **Why this exists.** `SESSION_TTL_SECONDS` is 15 minutes and a real
    orthopedic or dermatology visit routinely runs longer. TASK-006 shipped
    `/sessions/start` and `/sessions/{id}/end` and nothing in between, so a
    client whose token expires mid-visit has no way to obtain a fresh one for
    the session it is already in. It was found while specifying TASK-025 and
    opened rather than folded into it, because the fix is a service endpoint
    and TASK-025 is a screen.
  - **The failure this prevents.** The only move available to a client today is
    to call `POST /sessions/start` again. That creates a second `encounters`
    row with a new server-generated `session_id`, forking one visit into two
    encounters: the transcript splits across two `transcription:{session_id}`
    channels, TASK-030 writes two partial SOAP notes, TASK-060 assembles a
    bundle from whichever half it saw, and `procedure_seen:{session_id}` stops
    deduping across the visit so one procedure nudges twice. Nothing errors on
    that path, which is what makes it worth an endpoint rather than a warning.
  - `POST /sessions/{session_id}/token` — mints a new JWT with the **same**
    `session_id`, the encounter's existing `provider_id`, and a fresh `exp` from
    `SESSION_TTL_SECONDS`. Creates no row and mutates none. Response
    `{"data": {"session_id": ..., "jwt": ...}, "error": null}` per the envelope,
    **200 not 201** — nothing is created, which is the distinction from
    `/sessions/start`.
  - Semantics, mirroring `/sessions/{id}/end`: unknown or soft-deleted
    `session_id` → 404. An encounter whose `status` is already `completed` → 409,
    not a fresh token; a finished visit must not be able to reopen an audio
    socket. Publishes nothing — no consumer learns anything from a re-mint, and
    a second `sessions:started` would make TASK-021 re-subscribe to a channel it
    already holds.
  - **Authorisation was the open question this task had to settle. Settled:**
    the expired JWT is the credential, presented as `Authorization: Bearer` and
    accepted while it is no more than `SESSION_REMINT_GRACE_SECONDS` (default
    3600) past `exp`. Not a separate provider credential. The reasoning is in
    CLAUDE.md's "A visit outlasting the token re-mints" bullets and in
    `validate_remint_credential`'s docstring; do not re-derive it per client.
  - Reads and re-mints against an encounter, so it touches PHI: `audit_log()`
    per the route-level rule, with a distinct action from `START_SESSION`.
  - Update CLAUDE.md's "A visit outlasting the token re-mints" bullets when this
    lands — they currently say the endpoint does not exist and tell clients to
    surface `AUTH_REJECTED` in the meantime.
  - **Test:** a re-mint returns a token with the same `session_id`, the same
    `provider_id`, and an `exp` later than the original's
  - **Test:** the `encounters` row count is unchanged across a re-mint
  - **Test:** re-minting a completed session returns 409 and no token
  - **Test:** re-minting an unknown or soft-deleted `session_id` returns 404
  - **Test:** the minted token is accepted by TASK-020's WebSocket validator
  - Built (track-a-clinical 115 tests, 100% coverage; audio-ingestion 116).
    Decisions worth knowing before touching this:
    - **The credential decision, in one line:** a re-mint endpoint should be
      exactly as strong as the sockets its tokens open, and no stronger.
      `validate_token()` in audio-ingestion proves only possession, no provider
      authentication exists in the repo, and `/sessions/start` takes
      `provider_id` as an unauthenticated body field — so requiring more to
      refresh a token than to use one would be ceremony blocking on Phase 5
      infrastructure. The grace window bounds how long one *captured* token
      stays useful, which matters because nothing auto-completes an abandoned
      encounter.
    - **`SESSION_REMINT_GRACE_SECONDS=3600` is an assumption, not a
      measurement.** It was accepted deliberately as a starting value and has
      not been validated against a real visit. It is bracketed rather than
      derived: above a backgrounded mobile app's realistic gap, well under the
      4h `procedure_seen:{session_id}` TTL. Revisit it once there is real
      client behaviour to look at; changing it is a config edit, and the unit
      tests assert the behaviour tracks the setting rather than the literal.
    - **Re-minting revokes nothing — tracked as issue #51.** No `jti`, no
      `iat`, no server-side token store, so every token issued for the session
      inside the window remains acceptable, including superseded ones. Recorded
      in the route docstring and the OpenAPI description rather than implied
      away. The issue carries the options for narrowing it; ending the encounter
      is the only revocation available today, and it is all-or-nothing.
    - The provider is read from the `encounters` row, never from the presented
      token's claim, so a re-mint cannot alter the identity the original token
      was issued for. There is a test for exactly that.
    - A bad credential is rejected **before** the encounter is looked up, so the
      endpoint returns 401 rather than 404 for an unknown session and is not a
      probe for which session ids exist.
    - Only the `Authorization: Bearer` carrier is accepted. The
      `Sec-WebSocket-Protocol` carrier exists because the native `WebSocket`
      constructor cannot set headers; a plain POST can.
    - **The fifth test lives in audio-ingestion, not here**, as
      `tests/unit/test_remint_token_contract.py`. `import src.auth` from
      track-a-clinical's suite resolves to whichever of the four services that
      still install a top-level `src` sorts first — the shadowing hazard
      CLAUDE.md names — so the test sits where that import is unambiguous. It
      calls the real issuer and the real validator with nothing in between.
      This required a **dev-only** dependency on `medauth-track-a-clinical` in
      `services/audio-ingestion/pyproject.toml`; nothing in its `src/` imports
      it and nothing should.
    - **`ci.yml` now selects the audio-ingestion job when track-a-clinical
      changes.** Without that the contract test would never run when the issuer
      moves, which is the one failure it exists to catch. The selection list is
      de-duplicated because two rules can now pick the same member.
    - `tests/unit/test_main.py`'s route-set assertion was widened to three
      routes; it is the test that would otherwise silently accept a fourth.

---

- [x] **TASK-007:** Move CI and local dev to Node 24
  - **Why now.** `ci.yml` pinned `NODE_VERSION: "20"`, and Node 20 reached end of
    life on 2026-04-30 — verified against the `nodejs/Release` schedule, not from
    memory. That is a supply-chain position, not a style preference: the runtime
    every JavaScript job executes on stopped receiving security patches four
    months ago. Node 24 is the current active LTS (it does not enter maintenance
    until 2026-10-20 and is supported to 2028-04-30), so it is the target rather
    than 22, which is already in maintenance.
  - It was found while building TASK-023 and deliberately left alone there: the
    bump re-runs every JavaScript job on a new runtime, including Expo's
    toolchain, and that does not belong inside an audio task.
  - **The version is written in `.nvmrc` and nowhere else.** `ci.yml` reads it
    with `setup-node`'s `node-version-file`, so a contributor's nvm/fnm and CI
    cannot disagree. Repeating the number in the workflow is the arrangement
    that produces a build passing on a laptop and failing in a pull request for
    reasons the diff does not show — the same failure mode the backing-service
    versions were consolidated into `docker-compose.yml` to avoid.
    `package.json`'s `engines.node` stays as a floor, which is a minimum npm
    enforces rather than a second pin of the same value.
  - **Unblocks jsdom 30.** TASK-023 had to hold `apps/web` at jsdom 29 because 30
    requires Node `^22.22.2 || ^24.15.0 || >=26.0.0`, which Node 20 cannot
    satisfy. The floor moves to `>=24.15` with the upgrade, because that is
    jsdom's real requirement rather than a round number.
  - Toolchain compatibility checked before committing rather than after: React
    Native 0.86 declares `^24.3.0`, Vite 8, Vitest 4 and ESLint 10 all accept 24,
    and `expo`/`jest-expo` declare no `engines` constraint at all.
  - **Test:** the whole CI matrix passes on the new runtime — that is the point
    of the change, and there is nothing else to assert that the suites do not
    already cover.
  - Built. Decisions worth knowing:
    - `.nvmrc` holds a bare major (`24`), not a patch version, so security
      patches arrive without a commit. The exact floor that matters — jsdom 30's
      `^24.15.0` — is expressed in `engines.node` as `>=24.15` instead, where it
      belongs: `engines` is the constraint npm actually enforces at install time.
    - The four `setup-node` steps read `node-version-file: .nvmrc`. The
      `NODE_VERSION` env var is gone rather than left pointing at the file, so
      there is nothing left to fall out of step with.
    - jsdom 30 is the only dependency the runtime bump unblocked. Nothing else
      in the JavaScript toolchain was held back by Node 20.
    - **The declared jsdom version was not the one running, and bumping it alone
      would have changed nothing.** `apps/web` declared jsdom 29; the suite
      executed on **jsdom 20.0.3**. npm hoists a single copy to the workspace
      root, and jest-expo's `jest-environment-jsdom@29` requires `jsdom@^20`, so
      20 won the root slot and 29 sat nested under `apps/web`. Vitest lives at
      the root too and resolves `jsdom` from its own location, so it loaded the
      hoisted 20 and never saw the nested copy. Verified by reading
      `navigator.userAgent` inside a real test run — jsdom stamps its version
      there — not by reading `npm ls`, which showed the declaration rather than
      the resolution.
    - **The fix is a root `devDependencies` entry for jsdom, not an `overrides`
      block.** A direct dependency of the workspace root always takes the root
      slot, so Vitest now resolves 30 while jest-expo keeps its own 20 nested
      beside it — each consumer gets a version it supports. An `overrides` entry
      was tried first and rejected: it forces one version on every consumer,
      which would have handed `jest-environment-jsdom@29` a major it does not
      declare support for, to fix a problem that is about placement rather than
      compatibility.
    - The lesson generalises past jsdom: in a workspace where two toolchains want
      the same library, what a `package.json` declares and what the runtime loads
      are different questions, and only the second one matters. Probe the running
      process when it matters.

- [x] **TASK-007b:** Run the OpenAPI drift test on spec-only changes
  - File: `.github/workflows/ci.yml` (the `detect` step)
  - **The gap.** Each of the three services with an HTTP surface ships
    `tests/unit/api/test_openapi_contract.py`, which compares the committed
    `docs/api/<service>.yaml` against the app's generated schema on routes,
    methods, status codes, required request fields and error codes. The
    change-detection step selected jobs from `services/**`, `packages/**` and the
    workspace root only — **nothing under `docs/` selected anything.** So the
    drift test ran whenever the application half moved and never when the spec
    half did, which is backwards: the spec is the half a human edits by hand and
    therefore the half that drifts.
  - **It was reachable, not theoretical.** Verified before fixing, by editing
    only `docs/api/track-a-clinical.yaml` — changing the documented port from
    8003 to 8009 — and watching
    `test_documented_port_matches_the_local_dev_table` fail while no CI job
    would have selected it. A spec-only pull request could land red on `main`.
  - **The fix is the filename convention, not a lookup table.** CLAUDE.md's API
    Design section already fixes specs at `docs/api/<service-name>.yaml`, so a
    loop over the existing `all_services` array maps spec to job with no second
    list to keep in step. A spec added for a service that has no job yet
    correctly selects nothing.
  - It sits outside the packages/services if-else rather than inside it, because
    it holds either way — a spec edit selects its service whether or not
    anything under `packages/` moved.
  - Found while building TASK-006b and deliberately left alone there: it is a
    pre-existing gap in CI, unrelated to the re-mint endpoint, and folding it in
    would have mixed two unrelated changes in one pull request.
  - **The selection rules were extracted to a tested script in the same task.**
    They had no automated test at all — not for the new rule and not for any
    rule that preceded it — and the first draft of this task left it that way,
    verifying by hand instead. That was the wrong call for logic whose failure
    mode is a green run that tested nothing, so it is closed here rather than
    deferred: `.github/scripts/detect-changed-members.sh` holds the rules and
    `detect-changed-members.test.sh` exercises them.
  - **Test:** `bash .github/scripts/detect-changed-members.test.sh` — 38 cases,
    covering each spec selecting its own service, the over-selection cases that
    matter (an unrelated `docs/adr/` file, a spec for a service with no job,
    `…yaml.bak`, a nested lookalike path, all selecting nothing), every
    pre-existing selection behaviour, and the four boolean outputs (`web`,
    `mobile`, `fhir_types`, `audio_wire`), which had no coverage before.
  - Built. Decisions worth knowing:
    - The de-duplication guard added in TASK-006b now covers a third overlapping
      rule; its comment was rewritten, since a change touching a service, its
      spec and its paired service selects the same member three times.
    - **The script is a pure function of the changed-path list.** Base-SHA
      resolution and `git diff` stay inline in the workflow, because they need
      the GitHub event context and a real repository. That leaves a deliberate
      coverage boundary: the all-zero-SHA fallback for a first push to a new
      branch is still untested, and covering it would need a fixture git
      repository.
    - **The extraction changed exactly one rule, on purpose.** A change under
      `.github/scripts/` now selects every member, because the selection logic
      cannot be trusted to select its own blast radius. Verified by diffing
      every grep pattern before against after — that addition is the only
      difference between them.
    - **The `detect-logic` job is unconditional and declares no `needs`.**
      Gating the detector's own test on the detector would mean a bug that makes
      it select nothing also skips the test that would have caught it.
    - The suite was mutation-tested rather than merely run: dropping the `$`
      anchor, removing the whole `docs/api` rule, and removing the
      track-a-clinical/audio-ingestion pairing each produce failures, so the
      cases genuinely bite.
    - Two checks assert the repo against the rules rather than the reverse:
      every `docs/api/*.yaml` must name a real service directory, and
      `ALL_SERVICES` must match `services/`. Both catch the silent-gap shape one
      level up — a spec or a service that quietly gets no CI at all.
    - **`ci-passed` was missing `audio-wire`**, found while adding
      `detect-logic` to its `needs`. A job absent from that list can fail
      without blocking a merge. Both are in the list now. Strictly outside this
      task's scope, kept because it is the same silent-hole defect one layer up.

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
    step_therapy_details: str | None}` — wrapped in the standard envelope.
    **Two fields changed in TASK-040 and this line is no longer current.**
    `nudge_message` became `str | None`, where `None` means there is nothing
    worth interrupting the consultation for — see CLAUDE.md, "The nudge trigger
    is the message", which also explains why the original always-a-string
    version made the field useless as a signal. And a `source` field was added,
    carrying which tier answered, because the emitter has to tell a real answer
    from the safe fallback and was otherwise left inferring it.
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
    - **Corrected in TASK-040: `gap_analysis` decided what to *say* while the
      nudge emitter separately decided *whether to speak*, and the two
      disagreed.** A payer that requires authorization but publishes no criteria
      we can find, and a plan whose only problem is step therapy, both compose a
      message asking the provider to act and both scored below the emission
      trigger — so the message was written and never shown. The judgement now
      lives in one place: `nudge_message()` returns `None` when there is nothing
      to say, and the emitter fires if and only if it was handed a message.
      Nothing downstream re-derives it from `missing_criteria` or `denial_risk`.
      Two things worth keeping in view if this is ever revisited: the defect came
      from deriving one decision twice, not from either derivation being wrong on
      its own; and the naive repair — triggering on a non-empty message string —
      nudges on every single query, because every branch of the original
      function returned prose, including "No prior authorization required".
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
    nothing in the system could supply them — see **TASK-024**, which supplied
    `cpt_code`, and **TASK-052b**, which supplies the other three at SMART
    launch and is what finally makes this path run end to end. So this task
    calls a seam,
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
    - **The dedup key was the canonical keyword, and TASK-024 moved it to the
      CPT code.** The guard is `SADD procedure_seen:{session_id}`, which reports
      first-add atomically in one round trip; a read-then-write pair would race,
      and Transcribe delivers stabilized results in bursts. `claim_procedure()`
      takes an opaque `procedure_key`, so that move changed the call site and
      not the guard — which is what it was left opaque for. Members are now
      prefixed `cpt:` or `keyword:` depending on whether a code resolved.
    - **A structural failure keeps its claim; a transient one gives it back.**
      `MissingQueryParameters` means every later mention would fail identically,
      so the claim stands and the warning naming the unresolved fields is logged
      once per procedure per session rather than once per segment. A timeout or transport error
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

- [x] **TASK-022:** Mobile audio capture (React Native)
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
  - **Two version pins are load-bearing; neither is obvious from the SDK.**
    `jest-expo` is `^57.0.4`, not `57.0.0`: that release declared a peer
    dependency on `@react-native/jest-preset@^0.85.0` while SDK 57 is on React
    Native 0.86, so `npm install` fails with ERESOLVE (expo/expo#47435, fixed in
    `57.0.1`). It matters more here than in a standalone app because CI runs
    `npm ci` at the workspace root, so the conflict fails the whole install and
    takes `web` and `fhir-types` down with `mobile`. No `overrides` entry is
    needed on a current pin — do not copy one from a blog post.
    And `jest` itself is pinned to **^29.7.0, not 30**: `jest-expo@57` depends on
    jest 29 internals throughout (`babel-jest`, `jest-snapshot`,
    `jest-environment-jsdom` all `^29.2.1`). Installing jest 30 hoists a v30
    runtime over v29 environments and every suite dies in `jest-runtime` with
    `clearMocksOnScope is not a function` — before a single test runs, so the
    failure names nothing about the code.
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
    - **The check runs before the socket is opened, not after**, and it runs
      twice. `AudioStream` publishes the rate it is actually delivering once
      `start()` resolves, so the order is: request permission → start the
      stream → compare the rate it reports → compare the first
      `AudioStreamBuffer` delivered → **only then** open the WebSocket. Until
      both comparisons pass, no socket exists and not one byte of audio has
      left the device. The second check is not redundant: the buffer is the
      audio that would really reach Transcribe, and the two disagreeing is
      itself a reason not to stream. The first exists because a device that
      delivers no buffer at all would otherwise leave the failure invisible.
      A reported rate of zero means the native side has not filled it in yet
      and is not treated as a mismatch.
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
  - Built (49 tests, 99% coverage). Decisions worth knowing before touching this:
    - **`expo-audio`'s real API differs from its documentation in two ways that
      changed the design.** `AudioStream.stop()` returns `void`, not a promise.
      And `AudioStream` exposes `sampleRate` and `channels` once started, which
      is what made the two-stage validation above possible — the docs describe
      only the per-buffer fields, so the earlier check would not have been
      written from the documentation alone. Read the installed `.d.ts`, not the
      docs page, before changing the capture sequence.
    - The docs are right about the important half: `int16` buffers are
      documented as little-endian, which is what Transcribe requires. The
      `isLittleEndian()` guard stays anyway, because the failure it catches is
      inaudible noise reaching the transcriber rather than a crash.
    - **`EXPO_PUBLIC_AUDIO_WS_URL` already existed** in `.env.example` from the
      TASK-001 scaffold; this task uses it rather than adding a second variable
      for the same thing. It is an origin with no path — the hook appends
      `/ws/audio/{session_id}`.
    - The chunking lives in `PcmFramer` as a plain class with no React and no
      I/O, because the frame boundary is the part most likely to be wrong in a
      way a test can catch. It copies each incoming buffer: the native layer is
      free to reuse the same `ArrayBuffer` on the next capture, and retaining it
      would let a later capture overwrite audio still queued. It was written
      here as `src/audio/framing.ts` and moved to `packages/audio-wire` by
      TASK-023, when the browser client became its second consumer — along with
      the format constants and the error vocabulary. Nothing about it changed in
      the move.
    - Audio captured while the socket is still connecting is held, then flushed
      on open — but only up to five seconds, after which capture fails with
      `SEND_BACKLOG_EXCEEDED`. Past that the connection is not coming, and the
      choice is between a visible failure and unbounded memory growth holding
      PHI. It has its own error code rather than sharing `STREAM_FAILED`
      because nothing was transmitted and the encounter never started, which is
      a different thing to tell a provider than a working connection that
      dropped partway through.
      **Five seconds is a round-number default, not a measured value** — no
      device handshake times have been observed, because no screen exists to
      produce them. It is bounded to be longer than any healthy handshake and
      shorter than a provider waiting minutes to learn nothing is recording.
      Safe to change once real connect-time data exists. It is also a byte cap
      rather than a timer, so its wall-clock meaning is a consequence of the
      capture format being fixed.
    - `App.tsx` is a placeholder on purpose. Wiring a half-built session screen
      here would be the exact failure TASK-025 exists to prevent: a screen that
      looks like it is recording when it is not.
    - RNTL 14 is async throughout — `renderHook` returns a promise and `act`
      must be awaited. An un-awaited `act` leaks pending work into the next
      test, which then renders with a null `result` ref and fails somewhere
      unrelated to the cause.
    - CI needed no change: `ci.yml`'s `mobile` job already existed and stopped
      no-opping the moment `apps/mobile/package.json` landed. Verified locally
      from a clean `npm ci`: lint, `tsc --noEmit` and 49 tests all pass.

- [x] **TASK-023:** Browser audio capture (React Web)
  - Prerequisite: TASK-020 (the WebSocket server this streams to — its wire
    contract is fixed and this task conforms to it), TASK-006 (mints the session
    JWT this task presents; this task neither mints nor refreshes it)
  - App: `apps/web` — **currently an empty `.gitkeep`.** This task scaffolds the
    Vite app before it can add a hook: `package.json`, Vite + React, TypeScript
    in strict mode, Tailwind, Vitest + React Testing Library, ESLint. The npm
    workspace entry already exists in the root `package.json`, and `ci.yml`'s
    `web` job already exists but no-ops on `if [ ! -f apps/web/package.json ]`
    — so the moment that file lands, CI starts running `lint`, `typecheck`,
    `test` and `build` and each has to exist and pass.
  - **Capture uses `getUserMedia` + `AudioContext` + `AudioWorkletNode`, not
    MediaRecorder.** An earlier draft of this task said MediaRecorder, in the
    same way earlier drafts said `expo-av` for TASK-022 and Node.js for
    `fhir-integration`: written without checking. MediaRecorder cannot produce
    what `audio-ingestion` forwards to Transcribe Medical, and the failure would
    not be a degraded stream — TASK-020 recorded that Transcribe answers audio
    it cannot read by *hanging rather than erroring*, so the encounter would
    simply never produce a transcript. Measured in Chrome 133, 2026-08-24:
    - **There is no raw PCM or WAV output.** `MediaRecorder.isTypeSupported` is
      false for `audio/wav`, `audio/wave`, `audio/x-wav`, `audio/pcm`,
      `audio/x-pcm`, `audio/L16`, `audio/raw` and `audio/flac`; only
      `audio/webm` and `audio/mp4` are true. Note the trap: `audio/webm;codecs=pcm`
      *is* supported and does record, so a check that stops at
      `isTypeSupported` concludes PCM is available. It is not — the bytes begin
      `1a 45 df a3`, the EBML magic, so it is PCM inside a WebM container, and
      the sizes say float32 **stereo** (96175 bytes per 250ms against 96000
      expected at 48kHz; int16 mono would be 24000).
    - **A `timeslice` chunk is not independently decodable.** Recording at 250ms
      and calling `decodeAudioData` on each chunk alone: every chunk after the
      first throws `EncodingError`, and only the whole sequence joined decodes.
      In one run the container header split so badly that chunk 0 was a single
      byte (`1a`) and chunk 1 began `45 df a3`. Forwarding these frame by frame
      sends Transcribe a stream of fragments.
    - **There is no sample-rate control.** `sampleRate` is not a MediaRecorder
      property and is not in the `MediaRecorderOptions` IDL at all — passing it
      to the constructor is silently ignored. The rate follows the source track,
      and feeding it a 16kHz source does not help either: that recording decoded
      back at 48000, because Opus always encodes at 48kHz.
  - The path that does work, and what each step is for:
    - `getUserMedia({ audio: { channelCount: 1, ... } })` for the microphone.
    - `new AudioContext({ sampleRate: 16000 })` — the browser's own resampler,
      which is what makes 16kHz reachable at all when the device captures at
      48kHz. Verified to honour the request and to read the rate back on
      `AudioContext.sampleRate`.
    - An `AudioWorkletNode` whose processor posts each 128-sample render quantum
      to the main thread. **Keep the processor a pass-through**: it copies the
      input channel and posts it, and nothing else. Every decision worth testing
      — conversion, framing, format comparison — belongs on the main thread,
      because `AudioWorkletProcessor` does not exist in jsdom and code inside it
      cannot be reached by this task's tests.
    - **The node reaches `context.destination` through a `GainNode` at zero
      gain.** A node with no outputs is *specified* to keep processing on its
      connected inputs alone, and Chrome does — measured. But that is one engine,
      and an engine that disagreed would produce no audio, no error, and nothing
      to diagnose from. Being reachable from the destination is what every engine
      pulls. The zero gain is what stops that path from playing the encounter
      back through the room's speakers, and the processor writes nothing to its
      output either, so the silence does not rest on the gain value alone.
    - `floatToInt16LE()` from `packages/audio-wire`, then `PcmFramer` from the
      same package for the 8000-byte frames. Web Audio works in normalised
      floats; the conversion writes little-endian explicitly rather than
      inheriting host byte order, so the browser needs no `isLittleEndian()`
      guard.
  - **The wire format is `packages/audio-wire`, not a second copy.** The
    constants, `PcmFramer` and the `AudioCaptureError` vocabulary were extracted
    from `apps/mobile` in this task, before the browser hook was written. Both
    apps import them; neither defines them.
  - **The two-stage rate check from TASK-022 applies here, one layer over.** The
    comparison runs before the socket is opened and it runs twice: once against
    `AudioContext.sampleRate` after construction, and once against the rate and
    channel count the first worklet message reports, which is the audio that
    would really be sent. Until both pass there is no socket and no byte has
    left the browser. Same terminal `SAMPLE_RATE_UNSUPPORTED` /
    `CHANNELS_UNSUPPORTED` states, same discriminated union, returned and never
    thrown.
  - **Capture that starts and then delivers nothing must fail, not hang.** An
    `AudioContext` created without sticky user activation starts *suspended*, and
    a suspended context reports no error — it simply never runs the worklet. The
    hook asks a suspended context to `resume()` but **does not await it**: with
    no activation that promise may never settle at all, so awaiting it would move
    the silent hang from Web Audio into `start()`. What decides the outcome is a
    deadline: if no quantum arrives within `FIRST_AUDIO_TIMEOUT_MS`, capture
    fails with `CAPTURE_TIMED_OUT`. Without it the hook sits in `starting`
    forever, which is the silent hang this whole error vocabulary exists to
    prevent.
  - **The session JWT goes in the subprotocol list, not a header.** The native
    `WebSocket` constructor accepts a URL and subprotocols and nothing else, so
    open the socket as `new WebSocket(url, ["medauth.session.v1",
    `medauth.jwt.${jwt}`])`. TASK-020's server accepts either carrier and echoes
    `medauth.session.v1` back; see CLAUDE.md, "How the JWT reaches a WebSocket
    endpoint". Do not rediscover this by finding that headers are unavailable.
  - A rejected token fails the upgrade rather than surfacing as an `onclose`
    with code 4401 — the same section says why — so treat a connection that
    never opens as an auth failure and re-mint the session before retrying.
  - Streams to `${VITE_AUDIO_WS_URL}/ws/audio/{session_id}` as **binary frames**.
    A text frame gets the connection closed with 1003 by TASK-020's server.
    `VITE_AUDIO_WS_URL` already exists in `.env.example` from the TASK-001
    scaffold — an origin with no path; the hook appends the rest. Do not add a
    second variable for the same thing.
  - Audio buffered while the socket is still connecting is flushed on open, up
    to `MAX_PENDING_BYTES`, after which capture fails with
    `SEND_BACKLOG_EXCEEDED` — the shared cap, for the reasons recorded against
    it in `packages/audio-wire`.
  - Stops the tracks, closes the socket and drops the pending buffer on session
    end and on unmount. Nothing is written to disk on any path; the token is
    never logged and never placed in the URL.
  - **Surfacing any of this to a provider is TASK-070's job.** This hook cannot
    block a "start visit" button that does not exist yet — the web session UI is
    TASK-070, the same relationship TASK-022 has with TASK-025. A caller that
    ignores the `error` state gets no audio and no socket, which fails closed.
  - **Test:** unit-test the hook in jsdom with `getUserMedia` and `AudioContext`
    mocked — `AudioWorklet` does not exist in jsdom, so the fake `AudioContext`
    exposes an `audioWorklet.addModule` that resolves and a node whose `port`
    the test drives directly. Assert exactly 8000-byte binary frames are sent,
    that a partial tail is not sent early, that a context rate disagreeing with
    the request fails before any socket is opened, that permission denial is
    surfaced, and that stop clears the buffer.
  - **Test:** the float-to-int16 conversion and the framing are tested as pure
    functions in `packages/audio-wire`, not through the hook. They are the part
    most likely to be wrong in a way a test can catch, and they need no DOM.
  - Built (27 tests, 97% coverage; the shared package adds 27 more). Decisions
    worth knowing before touching this:
    - **A worklet node with `numberOfOutputs: 0` really is pulled — in Chrome.**
      Verified against an oscillator source: 188 render quanta arrived in a
      second of audio, each 128 samples, with the worklet's own `sampleRate`
      global reporting 16000. That measurement is why the shipped graph does
      *not* rely on it. It holds in one engine, and the compatibility tables do
      not cover behaviour of this kind, so a browser that disagreed would give no
      audio and no error. The node therefore has one output and reaches the
      destination through a zero-gain `GainNode` — portable by construction
      rather than by measurement. The measured first-quantum latency from that
      run, effectively one render quantum, is what
      `FIRST_AUDIO_TIMEOUT_MS = 3000` is set several hundred times above.
      **The zero-gain graph itself has not been exercised in a live browser** —
      the extension's input events stopped reaching the page during the attempt.
      It is the conventional shape and is asserted in the jsdom tests, but it is
      an assertion about construction, not a measurement of audio flowing.
    - **The processor must not be inlined, and Vite inlines it by default.** It
      is imported with `?url`, it is under 2KB, and the default
      `assetsInlineLimit` is 4KB — so a production bundle carried it as
      `data:text/javascript;base64,...` and `addModule()` was fetching a data
      URL. Any `script-src 'self'` content security policy blocks that, and this
      app should have one. `build.assetsInlineLimit` now refuses to inline `.js`,
      which emits it as a hashed same-origin asset instead. Confirmed by reading
      the built bundle, not by assuming.
    - **`MediaStreamTrackProcessor` would have avoided the worklet entirely** —
      it hands `AudioData` frames to the main thread through a `ReadableStream`,
      with no second file and no separate global scope. It is Chrome-only, and
      this is a screen providers will open in whatever browser their practice
      standardises on, so it is not worth the portability.
    - `getUserMedia` rejections are split: `NotAllowedError` and `SecurityError`
      become `PERMISSION_DENIED`, everything else `CAPTURE_FAILED`. The
      distinction reaches a person — TASK-070 offers a route into browser
      settings for the first and a plain retry for the second. The rejection's
      own message is never surfaced; it can name device paths.
    - **`CAPTURE_TIMED_OUT` was added to the shared vocabulary in this task**,
      after the suspended-context hole was found. `apps/mobile` has the same
      shape of gap — `useAudioCapture` there waits on a first `onBuffer` that a
      wedged stream may never deliver — and closing it is **TASK-026**, not this
      task. The code lives in `packages/audio-wire` so both can use it.
    - `start()` awaits twice — `getUserMedia`, then `addModule` — so a provider
      can end the visit mid-startup. The graph is not built if the hook stopped
      while either was in flight; otherwise a live microphone would be left
      attached to a hook that believes it has stopped. There is a test for it.
    - Teardown calls `stop()` on every track rather than only dropping the
      stream. An un-stopped track leaves the browser's recording indicator lit
      after the encounter ends, which tells a provider the opposite of the truth.
    - `ENDIANNESS_UNSUPPORTED` cannot occur on this platform. The browser writes
      its own samples through `DataView.setInt16(..., true)`, so byte order is an
      argument rather than a property of the host. The code stays in the shared
      vocabulary because mobile can still emit it.
    - **jsdom was declared at 29, not 30**, because 30 requires Node >= 22.22 and
      `ci.yml` ran Node 20 at the time. TASK-007 moved the runtime to 24 and the
      declaration to 30 — and found while doing so that *neither* number had ever
      been the one executing: npm hoisted `jsdom@20` to the workspace root to
      satisfy jest-expo, and Vitest resolves `jsdom` from its own location at the
      root, so this suite ran on jsdom 20 throughout. See TASK-007 for the fix
      and for why a declared dependency is not evidence of a resolved one.
    - React 19, not the React 18 CLAUDE.md named before this app existed;
      `apps/mobile` is already on 19.2.x through Expo SDK 57. Zustand is listed
      in CLAUDE.md for this app but is not installed — TASK-070 brings it with
      the first state worth keeping. Tailwind is v4, configured through
      `@tailwindcss/vite` with no `tailwind.config.js`.
    - `App.tsx` is a placeholder on purpose, for the same reason TASK-022's is:
      a screen that looks like it is recording when it is not is the failure
      TASK-070 exists to prevent.
    - CI needed no change for `apps/web`: the `web` job already existed and
      stopped no-opping the moment `apps/web/package.json` landed. The
      `audio-wire` job is new and shipped with the package. Verified locally
      from a clean `npm ci`: lint, `tsc --noEmit`, tests and a production build
      pass for `apps/web`, `apps/mobile` and `packages/audio-wire`.

- [x] **TASK-026:** Mobile capture deadline — fail when no audio arrives
  - Prerequisite: TASK-022 (the hook this adds a deadline to)
  - App: `apps/mobile`
  - **What this closes.** `useAudioCapture` sets `starting`, calls
    `stream.start()`, and then waits for a first `onBuffer` to validate the
    format and open the socket. Nothing bounds that wait. A stream that starts
    successfully and then delivers no buffer — a microphone seized by another
    app, a route change mid-start — leaves the hook in `starting` indefinitely,
    with no error for TASK-025's screen to show. The provider sees a session
    that never begins and is told nothing.
  - The browser hook has the same shape of gap and closed it in TASK-023 with a
    deadline plus the `CAPTURE_TIMED_OUT` code, which already lives in
    `packages/audio-wire` and is shared. This task applies the same treatment
    here; it was opened rather than folded into TASK-023 because it changes a
    hook that has already shipped and deserves its own review.
  - There is no `resume()` equivalent on this side — the suspended-`AudioContext`
    half of the browser problem is browser-specific. Only the deadline applies.
  - Pick the timeout deliberately and record where it came from, per the same
    rule the five-second backlog cap follows: no device start-up latencies have
    been observed yet, so whatever lands is a bounded default until TASK-025
    produces real numbers.
  - Add `CAPTURE_TIMED_OUT` to TASK-025's list of codes needing distinct
    handling — nothing was recorded, so a plain retry is the right offer, which
    is different from the not-retryable format failures next to it.
  - **Test:** a stream that starts and never delivers a buffer settles in
    `error` with `CAPTURE_TIMED_OUT`, and opens no socket ✓
  - **Test:** a buffer arriving before the deadline cancels it, and a long
    encounter is never torn down by its own start-up timer ✓
  - Built. Notes:
    - **The timeout is `FIRST_AUDIO_TIMEOUT_MS = 8_000`, in the mobile hook, and
      it is a round-number default rather than a measured value** — the same
      standing as `MAX_PENDING_BYTES` and for the same reason: no device
      start-up latencies exist yet, and TASK-025 is what will produce them.
      Deliberately *not* the browser's 3s. That number is anchored to something
      measured — first Web Audio quantum within ~8ms once the context runs —
      and bounds a path that never leaves the renderer. `stream.start()`
      resolving only means the OS audio subsystem accepted the request, and on
      a Bluetooth headset the input route is still being negotiated after it,
      so reusing 3s would import a justification about a different platform.
      Eight because the two ways of being wrong cost differently: too short
      tears down a capture that was about to work and reports nothing was
      recorded, which on reliably-slow hardware is a product that never records
      and a retry that never helps; too long only makes the provider wait
      longer for an error that is still actionable.
    - **Each platform declares its own deadline; there is no shared constant.**
      One number could only be right for one of them, since the browser bounds
      an in-process Web Audio path and the device bounds the OS audio
      subsystem. `CAPTURE_TIMED_OUT` stays shared in `packages/audio-wire` —
      its doc comment was widened to carry both platforms' causes, since it had
      been written entirely in terms of a suspended `AudioContext`.
    - **The uncovered path was the one with no rate reported.** `start()`
      checks the format only `if (reported.sampleRate)`, and zero — the native
      side not having filled it in yet — falls straight through to waiting for
      a buffer. That is the branch with nothing bounding it, so it has its own
      test rather than being assumed covered by the general case.
    - **`fail()` in the reported-format check needed an explicit `return`.** It
      was the last statement in `start()` and so fell out of the function on
      its own; with the deadline armed below it, dropping through would arm a
      timer on an already-torn-down hook and replace an accurate
      `SAMPLE_RATE_UNSUPPORTED` — not retryable on this hardware — with a
      `CAPTURE_TIMED_OUT` that invites the retry. Tested directly.
    - Disarming lives at the top of `teardown()` rather than at each call site,
      so every failure path, `stop()` and the unmount cleanup all get it.
    - The mobile suite ran entirely on real timers; these five tests install
      `jest.useFakeTimers()` and an unconditional `jest.useRealTimers()` in
      `afterEach` keeps a failure inside one from leaking a frozen clock into
      the next test. 38 tests pass, coverage 99% statements / 94% branches.

- [x] **TASK-024:** Policy query parameters — encounter state and procedure codes
  - Prerequisite: TASK-021 (defines the seam this task fills in), TASK-005
    (owns the `encounters` migration this task adds a column to)
  - **What this closes.** TASK-021 detects a procedure in a live transcript and
    can name it, but `POST /policies/query` also needs `payer`, `plan_type`,
    `state` and `cpt_code`, and nothing supplied them. The gap was found while
    building TASK-021 and deliberately left open there rather than papered over:
    `policy_dispatch.resolve_query_parameters()` raised `MissingQueryParameters`
    on every call and the consumer logged it once per procedure per session.
    This task replaces that function body. **It closes `cpt_code` and nothing
    else of those four** — `payer`, `plan_type` and `state` are TASK-052b's,
    see the scope note below. It also removes `provider_id` from that list,
    which was never genuinely missing.
  - **Nothing here may be approximated.** The Redis cache key is
    `rag:{payer}:{plan_type}:{state}:{cpt_code}`. A guessed CPT code does not
    merely return a poor answer for one encounter: it writes a real, cacheable
    policy answer under a key that stands for a different procedure, and the
    next encounter matching that key is served it. The failure is silent and
    crosses patients, which is why this is its own reviewed task.
  - **Scope: the payer columns are not part of this task.** `payer`,
    `plan_type` and `state` are populated from a FHIR `Coverage` resource at
    SMART launch, which needs TASK-051 and TASK-052 — neither of which exists.
    That population is **TASK-052b**, and this task is deliberately shipped
    without waiting for it rather than sitting idle: the migration, the code
    mapping, the dedup-key move and the audit decision are all independent of
    it, and the same "build behind a seam and be honest about what is stubbed"
    pattern is what created this task out of TASK-021. No placeholder value was
    invented in the meantime, for the reason above. So
    `resolve_query_parameters()` still raises for every real encounter — but it
    now names the fields genuinely absent for *that* encounter rather than a
    fixed list, so the warning keeps meaning something as TASK-052b fills them
    in one at a time.
  - **Add `state` to `encounters`** via a real Alembic migration in
    track-a-clinical (`alembic_version_track_a_clinical`, per CLAUDE.md).
    Two-character USPS code, nullable — it is unknown until an EHR launch
    supplies it. Note it is *not* the payer's jurisdiction: TASK-013 already
    normalises CMS's sub-state codes (`DN`/`QN`/`UN`, `NF`/`SF`, `EM`/`WM`,
    `CNMI`) to a parent state on the ingestion side, and this column is the
    other half of the comparison, so it must use the same vocabulary.
  - **`provider_id` was never missing.** An earlier draft of this task listed it
    among the unresolved parameters and it sat in `UNRESOLVED_PARAMETERS`
    alongside the four real gaps. It has been a non-null column on `encounters`
    since TASK-005 and needed nothing but a `SELECT`. Corrected here rather than
    left to look like a design constraint, the same way the Node.js
    `fhir-integration` and `expo-av` lines were.
  - **Design the keyword-to-CPT mapping.** Built as
    `services/track-b-rag/src/track_b_rag/procedure_codes.py`. The four
    questions this task posed, and the answers taken:
    - *Which code, when a keyword covers many?* A qualifier drawn from a closed
      vocabulary per keyword — a body site for imaging, a modality for a stress
      test — matched against the excerpt and resolved to the qualifier nearest
      the keyword, so "the shoulder is fine, let's MRI the knee" is a knee MRI.
      The longer of two overlapping matches wins, so "abdomen and pelvis" is its
      own code rather than the abdomen one nested inside it.
    - *What happens when the mapping is ambiguous or absent?* No query, and a
      log line naming the keyword. An entry exists only where the spoken phrase
      pins the code down to the level payers publish criteria at; where an
      unstated axis would change the authorization answer there is no entry.
      Where an entry does fix an unstated axis it names it in `assumes` rather
      than leaving it implicit. Eight of the ten detector keywords resolve.
    - **The qualifier axis is not always a body site**, and reading it that way
      is what made a first pass at this task exclude arthroscopy and injection
      outright. MRI and CT turn on anatomy, a stress test on modality, an
      arthroscopy on the intervention the surgeon plans, and a joint injection
      on joint size — and in both of the excluded cases the axis *was* spoken,
      it just was not the one being looked for. Both are mapped. Worth knowing:
      an arthroscopy needs no second axis for the joint, because the
      intervention implies it — a rotator cuff is a shoulder, a meniscus is a
      knee. A bare "torn meniscus" still resolves nothing, because trimming and
      repairing it are different codes and which one has not been decided yet.
    - **X-ray and biopsy remain unmapped, as a decision rather than a gap.** An
      X-ray's code is chosen by view count at the machine, and plain radiography
      is essentially never prior-auth gated — so a query would spend a Qdrant
      search and a Sonnet call to return a retrieval miss indistinguishable from
      a corpus we do not hold, which is the exact ambiguity `packages/payer-vocab`
      exists to prevent elsewhere. "biopsy" spans body systems with unrelated
      code families: the gated ones (breast) split by imaging guidance, and the
      ones our target specialties order most (skin) by technique. Neither is
      spoken. **TASK-044** is the alternative for these two — a nudge driven by
      the keyword alone — and it is a product question, not a mapping one.
    - *How does a new specialty extend it?* By adding rows to a curated table
      matched deterministically — the same shape `packages/payer-vocab` uses,
      so extending never means making the matcher cleverer. It still needs a
      deploy, which is a product problem and not only a code one; **TASK-024b**
      tracks putting the table behind a loader so a practice can extend it.
    - *Where does it live?* A module in track-b-rag, because it has one consumer
      today. `packages/api-envelope` was extracted when a second consumer
      appeared and not before, and prior-auth (TASK-060) is that trigger here.
      Unlike a payer slug, a CPT code is an external identifier this repo does
      not mint, so nothing stored depends on where the table lives.
  - **"No code" is four answers, not one**, because they are not equally fixable
    and an operator reading a log line has to tell them apart: a keyword that
    names no coded procedure at all (a biologic is a drug; a referral is
    administrative), one whose code turns on something never spoken, a missing
    qualifier, and a recognised qualifier with no entry yet. Only the last means
    *extend the table*.
  - **The CPT table is not clinically verified, and CPT is AMA-licensed.**
    Descriptors in the module are short paraphrases rather than the AMA's own.
    Nothing can query on the table yet, because the payer columns are still
    empty — so a certified coder's review and a licensing decision are
    prerequisites on **TASK-052b**, which is the task that makes any of it live.
    Do not close TASK-052b without them.
  - **Move TASK-021's dedup key to the CPT code.** Done:
    `policy_dispatch.procedure_key()` returns `cpt:{code}` where a code resolves
    and `keyword:{keyword}` where it does not, and the consumer claims that
    instead of the bare keyword. A knee MRI and a hip MRI are both 73721 and now
    hold one claim between them, so one order raises one nudge. `claim_procedure()`
    was left taking an opaque `procedure_key` for exactly this, so the guard
    itself is unchanged. The prefixes keep a keyword claim from ever colliding
    with a code claim and make the set self-describing when read out of Redis.
  - **TASK-040 adds `Encounter.id` to that guarded SELECT**, because
    `clinical_nudges.encounter_id` is a foreign key and the consumer holds only
    a `session_id`. Both halves of the guard below — the `_Row` fake that
    carries exactly the columns the SELECT asks for, and the compiled-SQL
    assertion that no patient column appears — were updated deliberately in
    that task rather than being allowed to pass by coincidence. A primary key
    is a row identifier, not a patient attribute, so the compliance answer the
    guard protects is unchanged. That is the guard working, not the guard being
    weakened; the columns it exists to keep out are still out.
  - **Decided: reading `encounters` to build a query is not a PHI access**, so
    it writes no `audit_log()` row and the audit obligation stays where it is —
    one row per `/policies/query` call, written by the route. The decision is
    enforced by the query rather than by convention: the `SELECT` names
    `provider_id`, `insurance_payer`, `insurance_plan_type` and `state` and
    nothing else, and an integration test asserts the emitted SQL so that a
    later `select(Encounter)` cannot quietly turn it into a PHI read. Known
    Constraints #6 says to flag rather than guess; this is the flag, resolved.
  - **A database failure is not a structural one.** `MissingQueryParameters`
    keeps the dedup claim, which is right for "this can never work" and wrong
    for a dropped connection — that would silence the procedure for the rest of
    the visit. Database exceptions propagate untouched so the consumer releases
    the claim.
  - **Test:** a transcript naming "MRI of the left knee" resolves a knee MRI CPT
    code, not a generic one ✓
  - **Test:** a keyword with no confident code mapping produces no code and a
    reason naming which of the four kinds of "no" it is — never a placeholder ✓
  - **Test:** the migration adds `state` and the model round-trips it, against a
    real migrated database ✓
  - **Test:** a fully populated encounter resolves every parameter, and an empty
    one names exactly the three columns TASK-052b fills ✓
  - **Test:** the emitted `SELECT` reads no patient column ✓
  - Built. The end-to-end test over Redis — a published transcript segment
    producing a real `/policies/query` call — is **TASK-052b's** acceptance
    criterion, because it cannot pass until the payer columns are populated.

- [ ] **TASK-024b:** Extensible procedure code table
  - Prerequisite: TASK-024 (builds the table this moves)
  - Service: `services/track-b-rag`
  - **Why this exists.** TASK-024 asked how a new specialty extends the
    keyword-to-CPT mapping, and the answer it shipped is "add rows and deploy".
    That is fine for adding orthopedic and dermatology coverage ourselves and
    wrong as a product answer: a dermatology practice whose common procedures we
    have not coded gets silence, and cannot do anything about it. MedAuth
    targets exactly those specialties first (see the EHR priority order), so
    this is a customer-facing gap and not only a code one.
  - Move `KEYWORD_RULES` behind a loader so the table is data rather than
    source, and decide deliberately where practice-specific rows live — a
    config file, a Postgres table keyed by organization, or both. An
    organization-scoped table is the likely answer, since `encounters` already
    carries `organization_id`.
  - **A practice-supplied row is subject to the same rule as ours**: an entry
    exists only where the spoken phrase determines the code. A tenant able to
    map "MRI" to one code for everything would reintroduce precisely the cache
    collision TASK-024 exists to prevent, on their own data and ours — the
    `rag:` key is not scoped per organization.
  - Keep the four refusal reasons; a loaded table changes where rows come from,
    not what "no code" means.
  - **Test:** a practice-scoped row resolves for that organization and not for
    another
  - **Test:** a row that would collapse two procedures onto one code is rejected
    at load, naming the conflict

- [x] **TASK-025:** Mobile session screen
  - Prerequisite: TASK-006 (`POST /sessions/start` and `/end`), TASK-006b
    (`POST /sessions/{session_id}/token`), TASK-022 and TASK-026 (the capture
    hook this screen drives, and the deadline that makes it fail rather than
    hang)
  - App: `apps/mobile`
  - **Why this exists.** TASK-022 builds `useAudioCapture` and nothing calls
    it. `apps/mobile` has no session UI at all — only the capture hook and
    TASK-043's haptic nudge — so the mobile half of the "start visit" flow is
    missing. It was found while specifying TASK-022's sample-rate failure and
    opened rather than folded into that task.
  - Start visit: `POST /sessions/start` (TASK-006, returns **201** with
    `{session_id, jwt}` in the standard envelope), then hand both to
    `useAudioCapture`.
  - **This screen does not source the patient, and must not invent one.**
    `POST /sessions/start` requires `patient_id` and a `provider_id` UUID and
    nothing on mobile can supply either today: fhir-integration exposes no
    patient search — TASK-052 defines `GET /fhir/patient/{patient_id}/context`
    and `GET /fhir/encounter/{encounter_id}` only — and no provider
    authentication exists anywhere in this repo before Phase 5. Build against a
    seam: one injected function returning the two identifiers, with test
    doubles behind it. Filling that seam is **TASK-025b**. Do not add a picker
    over an endpoint that does not exist, and do not hardcode a Synthea patient
    id as a default — a hardcoded id is indistinguishable from a real one at
    runtime and would file an encounter against the wrong patient.
  - **The HTTP base URL is `EXPO_PUBLIC_API_BASE_URL`**, which already exists
    in `.env.example` and is unused so far, so this task adds no new variable —
    it adds the export to `apps/mobile/src/config.ts` beside the audio origin.
    It is **not** `EXPO_PUBLIC_AUDIO_WS_URL`, which is a `ws://`/`wss://` origin
    that `useAudioCapture` appends `/ws/audio/{session_id}` to; reusing it would
    put a WebSocket scheme in front of a REST path. The same TLS rule applies —
    the start-visit body carries a patient identifier.
  - **The screen must not reach an "in progress" state while the capture hook
    is in `error`.** This is the visible half of TASK-022's contract: any error
    state blocks the visit from starting and is shown to the provider as an
    actionable message. A provider who believes an encounter is being recorded
    when it is not is the worst outcome this screen can produce — worse than
    refusing to start — because the transcript, the SOAP note and every nudge
    that should have fired are all silently absent.
  - **That rule covers the whole error vocabulary, not only the codes broken
    out below.** `AudioCaptureErrorCode` in `packages/audio-wire` is the full
    set, and every member of it blocks the visit. A code with no branch of its
    own still renders its own `message` and a retry offer — never a silent
    fallthrough, and never an in-progress screen. These are the ones where the
    right response to the provider differs:
    - `PERMISSION_DENIED` — a settings problem. Offer the route to fix it.
    - `SAMPLE_RATE_UNSUPPORTED` / `CHANNELS_UNSUPPORTED` — the device cannot
      capture what Transcribe needs. Not retryable on this hardware; say so
      rather than inviting a retry loop.
    - `ENDIANNESS_UNSUPPORTED` — the same class of failure and mobile-only: the
      platform is big-endian, so the PCM would reach Transcribe byte-swapped.
      Not retryable on this hardware either, and it is a property of the device
      rather than of this visit.
    - `CAPTURE_FAILED` — the microphone refused to start and said so. Retryable,
      and worth distinguishing from `CAPTURE_TIMED_OUT` in what the provider is
      told: something failed, rather than everything reporting success and no
      audio arriving.
    - `CAPTURE_TIMED_OUT` — the microphone started and then delivered nothing.
      Nothing was recorded, so a plain retry is the right offer, unlike the
      format failures above which are not retryable on this hardware. The code
      was added to the shared vocabulary by TASK-023 (the browser hook hit the
      gap first); TASK-026 is what makes the *mobile* hook emit it.
    - `AUTH_REJECTED` — the session token was refused before the handshake
      completed. Re-mint for the **same** `session_id` through
      `POST /sessions/{session_id}/token` (TASK-006b) and retry, rather than
      reusing the rejected token. Never `POST /sessions/start`, which forks the
      encounter in two; a 409 from the re-mint means the encounter is already
      completed and is the one case where the provider is asked to start a new
      visit. The CLAUDE.md citation below is where this is decided — this
      bullet restates it, and must not diverge from it.
    - `SEND_BACKLOG_EXCEEDED` — the socket never opened and buffered audio hit
      its cap. Nothing was recorded and the encounter never started, so a plain
      retry is the right offer.
    - `STREAM_FAILED` — a socket that had opened and was carrying audio failed
      or closed. Part of the encounter did reach the server and part did not, so
      the provider is told the recording is incomplete rather than that nothing
      was captured. It is the one code here where retrying does not restore a
      whole encounter.
  - End visit: `POST /sessions/{session_id}/end`, stop capture, clear buffers.
  - **A visit outlasting the 15-minute JWT is settled in CLAUDE.md**, under
    "A visit outlasting the token re-mints" in the Session Lifecycle & JWT
    Issuance section. That section is the shared source for both session
    screens; this task cites it and does not re-derive it. It is no longer an
    open question: the encounter never ends because a token expired, an
    already-open socket is unaffected because validation is handshake-only, and
    re-minting happens for the *same* `session_id` — never by calling
    `POST /sessions/start` again, which forks the encounter in two.
  - **TASK-006b has shipped** `POST /sessions/{session_id}/token`, so this
    screen refreshes rather than surrendering: call it before opening any new
    socket when the held token is near `exp`, and again on `AUTH_REJECTED` from a
    socket that failed to open. A 409 means the encounter is already completed
    and the visit really is over — that is the only case where the provider is
    asked to start a new one. Still never re-call `/sessions/start` to get a
    token; that forks the encounter.
  - **Test:** capture reports `SAMPLE_RATE_UNSUPPORTED`, verify the visit does
    not start and the error is rendered ✓
  - **Test:** permission denied, verify the same ✓
  - **Test:** an error code with no dedicated branch — `STREAM_FAILED` — still
    blocks the in-progress state and renders its own message ✓
  - **Test:** `AUTH_REJECTED` re-mints via `POST /sessions/{session_id}/token`
    and never calls `/sessions/start`; a 409 from that re-mint asks the provider
    to start a new visit ✓
  - **Test:** start/active/end transitions with mocked APIs. `apps/web`'s
    equivalent (TASK-070) is not built, so there is nothing to mirror — the
    shared behaviour comes from the CLAUDE.md section cited above, and this
    screen is the first of the two to implement it. ✓
  - Built. Notes:
    - **The "never in progress while capture has failed" rule is a pure
      function, `src/session/visitPhase.ts`.** It returns `recording` only when
      the hook reports `streaming`, and there is no other path to it, so the
      rule can be tested over every code in the vocabulary instead of over the
      branches someone remembered to write. `src/session/recovery.ts` holds the
      part that does vary per code — what the provider is offered next — behind
      a switch that is exhaustive over `AudioCaptureErrorCode`. A code added to
      `packages/audio-wire` fails typechecking there until someone decides what
      to say about it, which is the compile-time form of the same rule.
    - **The patient seam is `src/session/patientSource.ts`**, and `App.tsx`
      wires it to `patientSelectionUnavailable`, so this build shows a provider
      that a visit cannot be started rather than starting one against an
      invented identifier. TASK-025b fills it.
    - **Two ordering details in the screen are load-bearing.** After a re-mint
      the capture hook still holds the old token in the render the effect
      belongs to, so the effect bumps an attempt counter and re-enters rather
      than starting capture against a token that is about to be replaced; the
      counter is a counter and not a boolean because a retry and the second pass
      after a refresh both need to re-enter a start that has already run. And a
      visit gets at most one proactive refresh per attempt, so a token that
      arrives already near `exp` cannot loop.
    - **Ending stops the microphone before closing the encounter**, because the
      provider has said the visit is over and audio must not keep being captured
      while that call is in flight. An end that fails keeps the session held and
      offers to retry the end — offering a new visit there would fork the
      encounter, which is the failure the whole re-mint path exists to avoid.
    - `EXPO_PUBLIC_API_BASE_URL` needed no addition to `.env.example`; it has
      been there since TASK-001 and this is its first consumer. The comment
      block beside it now says what it is for and why the audio origin is not
      interchangeable with it.
    - `src/api/jwt.ts` decodes base64url by hand. Hermes does not guarantee
      `atob`, and the test fixtures encode by hand for a matching reason — the
      app has no `@types/node` and adding it to build a token would let Node-only
      globals typecheck inside `src/`.
    - **A leftover from TASK-022 was corrected here**: the hook's
      `AUTH_REJECTED` message told the provider to start a new session, written
      before TASK-006b existed. The screen would have rendered that directly
      above its own contradicting offer to refresh. The message now reports the
      failure and leaves the response to the screen.
    - CI needed no change — the `mobile` job already covers `apps/mobile/**`.
      131 tests pass with 94% coverage against the 80% gate, and each commit in
      the series typechecks and passes its own suite.

- [ ] **TASK-025b:** Real patient and provider selection on mobile
  - Prerequisite: TASK-025 (the seam this fills), TASK-052 (base FHIR resource
    fetching)
  - App: `apps/mobile`; also `services/fhir-integration` for the search route
  - **The gap.** TASK-025 starts a visit through an injected function returning
    `{patient_id, provider_id}`, because neither identifier has a real source
    yet. The seam let that screen ship with its refusal behaviour intact; this
    task gives it something real to call.
  - `GET /fhir/patient/search?query=` **does not exist.** TASK-052 defines the
    context and encounter reads only. Add the route in fhir-integration — it is
    the same route TASK-070 needs, which flags it as possibly missing rather
    than assuming it, so it is built once in the service and not per app.
  - It reads patient demographics, so it audits through hipaa-logger like every
    other route in that service.
  - Provider identity still arrives with SMART on FHIR in Phase 5. If that has
    not landed, fill the patient half and say so rather than inventing a
    provider.
  - Until this lands, no build of `apps/mobile` can start a real encounter, and
    that is deliberate — see the hardcoded-id argument in TASK-025.
  - **Test:** a search returns matches and the selection populates the
    start-visit call
  - **Test:** TASK-025's session screen tests still pass with the real
    implementation substituted for the seam

---

## Phase 3 — Clinical Note Generation (Track A)

- [x] **TASK-030:** Transcript accumulation + SOAP generation
  - Prerequisite: TASK-006 (session-end signal), TASK-020/021 (transcript events)
  - Service: `services/track-a-clinical`
  - This is also the Track A half of TASK-021's fan-out, which that task
    deliberately left to be built here. Subscription discipline is inherited
    from it and not re-derived: subscribe per session on `sessions:started`,
    **never** by pattern on `transcription:*`, and release on
    `session:ended:{session_id}`. `track_b_rag/transcript_consumer.py` is the
    working precedent for the shape, including the restart gap below.
  - Subscribe to `transcription:{session_id}` — accumulate segments into a rolling
    transcript buffer per session, in-memory (not persisted mid-session; the
    session is short enough that a service restart mid-encounter is an accepted
    edge case for v1, not something to build recovery for yet). A restart
    therefore loses the visits in flight, exactly as TASK-021's consumer does,
    and logs at WARNING naming the count rather than pretending otherwise.
  - Subscribe to `session:ended:{session_id}` (published by TASK-006) — on
    receipt, call Claude **Sonnet** via Bedrock with the full accumulated
    transcript (see CLAUDE.md "Bedrock Model Assignment" — SOAP generation is
    Sonnet; the ICD/CPT extraction pass below is Haiku)
  - Prompt: generate SOAP note. Separately, run a Haiku extraction pass for
    ICD-10 codes and anticipated CPT codes — two calls, not one, so the cheap
    model handles the mechanical extraction and the expensive model focuses on
    the clinical writing
  - Store result in `clinical_notes` table. The `icd10_codes` and `cpt_codes`
    JSONB columns take the object shape fixed in CLAUDE.md "Extracted clinical
    codes — one JSON shape": every entry the Haiku pass writes is `source:
    "llm-extraction"` with `confidence: null` and `validation: null`, and
    TASK-031 fills the validation half in place. Do not write bare code strings.
  - **Codes only Comprehend Medical found are written here too, from the same
    request TASK-031 already makes.** `InferICD10CM` returns every ICD-10-CM
    concept it reads in the transcript, so its response answers two questions at
    once: whether each code Haiku proposed holds up, and what Haiku missed. The
    second half was briefly its own task (TASK-031b) and is folded back in
    because it is not a second call, a second seam or a second pass — it is the
    other half of one response, and splitting it would have meant a second round
    trip to AWS to learn what the first already said.
    - Written as their own entries with `source: "comprehend-medical"` and a
      real `confidence` — the `ICD10CMConcept.Score`, the field an
      `llm-extraction` entry structurally cannot carry — appended after the
      LLM's own entries so a suggestion never displaces or reorders what the
      extraction pass proposed.
    - **The product question this was gated on is settled in CLAUDE.md**, in the
      shape contract, because it binds TASK-060 and TASK-071 rather than this
      task alone: such an entry is a *suggestion*, not a stated diagnosis.
      TASK-071 renders it as one, and TASK-060 does not claim it in a
      prior-auth bundle — a bundle asserts to a payer what the provider
      documented, and a code nobody stated is not that. It becomes claimable
      the ordinary way, by a provider accepting it through TASK-032's edit.
    - **A proposal threshold, separate from TASK-031's confirmation
      threshold**, and provisional in the same way and for the same reason.
      `InferICD10CM` returns several candidate concepts per detected entity and
      most are wrong, so a bar is what keeps a clinical note from becoming a
      candidate list. The two constants hold the same value today and are
      deliberately not one name: asserting a code nothing in the documentation
      asserted is a stronger claim than confirming one a second source already
      proposed, and they are expected to diverge once a real distribution of
      scores exists.
    - **One diagnosis is one entry.** Nothing is appended alongside a code
      already in the list, whatever its source, and the comparison goes through
      the dotless matching key — otherwise `M1711` from Comprehend and `M17.11`
      from Haiku become two entries for one diagnosis, which is the payer-slug
      failure arriving from the other direction.
    - **`[]` is reconciled; `null` is not.** An extraction pass that ran and
      found nothing is exactly where a code only Comprehend read is worth
      surfacing. An extraction pass that never answered leaves `null`, and
      filling that with suggestions would replace "not determined" with a list
      that reads as determined — the collapse `validation: null` exists to
      avoid, one level up.
    - **Test:** a transcript naming a diagnosis the LLM pass missed yields an
      entry sourced to comprehend-medical with its score; a weakly-linked
      candidate yields none; a code the LLM already proposed is never
      duplicated, in either spelling.
  - **Ordering on the end signal, in this order and for this reason.**
    Unsubscribe from the session's channels first, so no segment arrives during
    generation and no second end signal is processed. Then keep the in-memory
    buffer alive across the whole Sonnet call, the Haiku call and the database
    write, and drop it **only after the row is durably committed.** Structuring
    it the other way — releasing the buffer when the signal arrives, the way the
    Track B consumer releases its dedup claim — makes any mid-operation failure
    silent and unrecoverable: the transcript is gone, no note exists, and
    nothing anywhere records that an encounter produced nothing. Holding the
    buffer means a failed generation is retryable, which is safe precisely
    because the write is idempotent per the next bullet. Same reasoning as
    TASK-011's Qdrant-first/Postgres-second ordering. TASK-060's documented race
    against this task depends on the timing here, so it is stated rather than
    left to the implementation.
  - **`clinical_notes.encounter_id` gets a real UNIQUE constraint, added by
    migration in this task, and the insert is `ON CONFLICT DO NOTHING`.**
    TASK-060 already treats "at most one note per encounter" as true while
    nothing enforces it. Redis pub/sub redelivery, a consumer reconnect, or any
    retry of the bullet above would otherwise spend a second Sonnet call and
    leave a second row that TASK-060 was not built to see — and neither the
    duplicate call nor the duplicate row would raise anything. Same defensive
    write as TASK-011's `_record_policy`, with `DO NOTHING` rather than `DO
    UPDATE` because the first note generated for an encounter is the one to
    keep: a retry has no better information than the attempt it follows, and
    TASK-032's provider edits must not be silently overwritten by a late
    duplicate signal.
  - **Audit:** this consumer reads a whole encounter's speech and writes a PHI
    record with no request behind it. It audits `WRITE_NOTE` per CLAUDE.md
    "Auditing work that no request triggered" — `actor_id` from
    `encounters.provider_id`, one row per generated note and not one per
    segment, on the same transaction as the insert. A generation suppressed by
    `ON CONFLICT DO NOTHING` wrote no note and audits nothing.
  - **`GET /health` is added to this service in this task**, with
    `transcript_consumer` and `session_end_consumer` flags, matching
    track-b-rag's existing endpoint rather than inventing a second convention
    (envelope on 503, no `audit_log()` call, flags in `data`). Both flags
    because this task subscribes to both channel families. The justification is
    the one TASK-021 already made and is stronger here: a dead consumer stops
    every future SOAP note for every encounter on the pod, and nothing else in
    the system notices — TASK-060 would simply wait out its retries and log a
    warning per visit. track-a-clinical has no health endpoint at all today,
    which is its own gap.
  - **Config:** add `aws_region` and the Bedrock model ids
    (`BEDROCK_MODEL_ID_FAST`, `BEDROCK_MODEL_ID_REASONING`) to `Settings`
    alongside the existing `redis_url`. All three already exist in
    `.env.example`; the model id is never a literal in code.
  - **Test:** send sample orthopedic encounter transcript, verify SOAP structure returned
  - **Test:** publish `session:ended:{session_id}` with an accumulated transcript
    buffered from prior TASK-020 test data, verify clinical_notes row is created
  - **Test:** publish `session:ended:{session_id}` twice, verify exactly one
    `clinical_notes` row and exactly one Sonnet call
  - **Test:** a failing database write leaves the buffer held, and a retry of the
    same session still produces a note
  - **Test:** `GET /health` reports 503 naming the stopped consumer when the
    consumer task is not running
  - Built (227 tests, 100% coverage; 608 in track-b-rag still pass unchanged).
    Decisions worth knowing before touching this:
    - **`packages/bedrock-client` was extracted here**, because this service
      became the second Bedrock caller and the honest description of what it
      needed was track-b-rag's `bedrock.py`. Sharing without a package was not
      available: track-b-rag already imports `track_a_clinical.models`, so the
      reverse import would be a cycle. The package holds client construction
      plus the two functions that fail *quietly* when wrong — `message_text`,
      for `AIMessage.content` being a block list rather than a string, and
      `first_json_object`, the balanced-brace scanner. Each service keeps its
      own settings-coupled factory and its own cache, because its settings are
      what decide when a client is stale. Same argument as api-envelope in
      TASK-010.
    - **The two passes fail independently and only one of them is fatal.** No
      SOAP note means no row and a retained buffer. A failed extraction still
      stores the note, with `icd10_codes` and `cpt_codes` NULL rather than `[]`
      — the column then records that the codes were never determined instead of
      claiming the visit had none, which is the same distinction the `validation`
      field carries one level down.
    - **A duplicate end signal is guarded twice, at different costs.** The
      unique constraint stops the second row; an in-flight set keyed by session
      stops the second pair of LLM calls, which the constraint cannot. A
      redelivery arriving before the unsubscribe takes effect is the case only
      the second guard covers.
    - **Generation runs as its own task**, so one note being written does not
      stall transcript accumulation for every other encounter on the pod. On
      shutdown they are cancelled and the count is logged: their transcripts are
      in memory and go with the process.
    - `GET /health` reports `redis` and `transcript_consumer` and deliberately
      **not** a separate session-end flag. One task on one subscription serves
      all three channel families, so a second flag could never disagree with the
      first, and a probe that cannot fail independently implies coverage that
      does not exist. Split the end handling onto its own task and it earns one.
    - No new environment variables. `AWS_REGION`, `BEDROCK_MODEL_ID_FAST` and
      `BEDROCK_MODEL_ID_REASONING` were already in `.env.example`; this task
      only added them to `Settings`.
    - CI gained one entry: `bedrock-client` in `ALL_PACKAGES` in
      `detect-changed-members.sh`, with a case in its test. The `test` matrix is
      generated from that array, so no new job was needed and `ci-passed`'s
      `needs` is unchanged.
    - **The discovery half landed later, with TASK-031's branch**, once the
      Comprehend Medical request existed to carry it. It is one function with
      the validation pass — `comprehend.reconcile_icd10`, named for doing both
      rather than for the half it started as — because both answers come out of
      one `InferICD10CM` response and a second seam would have meant a second
      round trip to learn what the first already said. The consumer takes it as
      one injected seam, so no test in `test_consumer.py` can reach AWS. The
      UNVERIFIED note on TASK-031 covers this half too: the deduplication that
      stops one diagnosis becoming two entries runs through the same
      `matching_key`, so it is correct under either spelling and closes with the
      same live call.

- [x] **TASK-031:** Comprehend Medical validation layer
  - Prerequisite: TASK-030 (writes the entries this validates)
  - Service: `services/track-a-clinical`
  - Validate the ICD-10 codes from TASK-030's Haiku pass against AWS Comprehend
    Medical `InferICD10CM`, and record what it found in the `validation` half of
    each entry, per CLAUDE.md "Extracted clinical codes — one JSON shape". The
    column shape already has the field waiting, so **this task adds no
    migration**.
  - **ICD-10 only.** Comprehend Medical infers ICD-10-CM, RxNorm and SNOMED CT
    and has no CPT inference of any kind, so `cpt_codes` entries keep
    `validation: null` indefinitely. That is the designed outcome, not a gap this
    task should paper over.
  - **Scope: this task fills in the `validation` half only.** Codes Comprehend
    surfaced that the LLM never proposed are written by TASK-030's discovery
    half, from the same response, and the two are one function — see TASK-030.
    They were briefly split off as TASK-031b before that was folded back in;
    that task no longer exists.
  - **Runs before the insert, not as an update after it.** `store_note` is
    `ON CONFLICT DO NOTHING` and has no idempotent update path, so a validation
    pass running after the write would have nothing to update on the retry of a
    duplicated signal. Validating first means the row is written already
    validated, in one write, with no second transaction to get half-applied.
  - **The two passes fail independently and validation is never fatal.** Same
    framing as TASK-030's Sonnet/Haiku split: a Comprehend failure — throttling,
    an outage, a text-size rejection — leaves every entry's `validation` at
    `null` and **stores the note anyway**. The note is the artifact the provider
    is waiting for; validation is secondary metadata about it. Blocking a note
    on a metadata pass would trade the thing that matters for the thing that
    annotates it. `validation: null` already means exactly "not checked yet",
    so an unvalidated note is honestly represented with no extra signalling.
  - **The input is the transcript, not the generated note.** Validating the
    LLM's codes against text the same LLM wrote is closer to circular than a
    check — the note is where the model already committed to its reading of the
    encounter, so agreement there measures self-consistency rather than
    accuracy. The transcript is the independent source both passes derive from.
  - **Chunking against a 10,000-character cap, and never a silent truncation.**
    `InferICD10CM` accepts at most 10,000 characters — botocore enforces it
    client-side from the shape metadata and the service raises
    `TextSizeLimitExceededException`. This is **not** the 20,000-byte figure
    commonly quoted for Comprehend Medical, which belongs to `DetectEntitiesV2`;
    building against that number would size chunks at twice the real limit and
    fail on every long visit. A transcript over the cap is chunked and the
    results merged — a code confirmed in any chunk is confirmed. If any part of
    a transcript cannot be covered, that is logged at WARNING naming the
    shortfall, per CLAUDE.md "An accumulated transcript exceeds downstream
    limits". A partial validation presented as complete is the failure class
    this whole document rejects.
  - **The confidence recorded is `ICD10CMConcept.Score`, not the entity-level
    `Score`.** The response nests both, and the botocore service model separates
    their meanings: entity `Score` is confidence in the *detection* that a span
    is a medical condition, concept `Score` is confidence that the entity is
    *linked to that ICD-10-CM concept*. This comparison is code-to-code, so the
    concept score is the one measuring the question being asked — an entity
    score can be high for a correctly-spotted condition linked to the wrong
    code, which is the exact error this task exists to catch. Decided from the
    wire contract, independent of live data.
  - **Threshold `0.8` is a named constant, commented as an unvalidated initial
    guess.** It comes from this task's original wording, not from a measurement
    against real Comprehend output, and the comment must say so — same rule as
    `SESSION_REMINT_GRACE_SECONDS` in CLAUDE.md's session section. Revisit once
    a real response distribution exists.
  - **Code matching is isolated in one small function and its dot handling is
    UNVERIFIED.** ICD-10-CM has two standard spellings (`M17.11`, `M1711`) and
    nothing yet establishes which Comprehend returns; `ICD10CMConcept.Code` is an
    unconstrained string in the service model, so this cannot be settled from the
    contract — it is data, not schema. Both sides of the comparison therefore go
    through one normaliser producing a dotless key, per CLAUDE.md's
    "Storage is dotted, matching is dotless" rule. The function and its tests are
    marked UNVERIFIED and name `scratchpad/probe_real.py` as the way to close it.
    Keeping it to one pure function is the point: confirming the real format is
    then a change to that function, not a rework of the validation pipeline.
    **Do not guess a format and quietly drop the marker.**
  - **Testing cannot use moto** — it does not implement Comprehend Medical, per
    Known Constraints #3 and CLAUDE.md's standing exception. Use
    explicitly-labelled synthetic fixtures, stating in the fixture module that
    they are hand-written and that the code format in them is the unverified
    part. A silent `unittest.mock` patch presented as satisfying the moto rule is
    not acceptable.
  - **The sync boto3 call is wrapped in `asyncio.to_thread`**, per CLAUDE.md's
    rule on raw sync boto3 in async contexts. This runs inside the consumer's
    event loop alongside every other live encounter on the pod.
  - **`boto3` becomes a direct dependency of `services/track-a-clinical`.** It is
    currently only transitive through `medauth-bedrock-client`, and a direct
    client call must not rely on another package's dependency continuing to
    supply it.
  - **No audit row and no new env var.** Discrepancies are a quality metric, not
    a PHI access — log them through standard `logging` at WARNING, naming the
    code and the confidence score and **never the surrounding clinical text**.
    The transcript reaches Comprehend Medical, which is a HIPAA-eligible service,
    and nowhere else. `AWS_REGION` is already in `Settings` from TASK-030.
  - **Test:** run on a transcript with 3 clear diagnoses, verify codes match
  - **Test:** a code Comprehend did not return at all gets `validation` present
    with `confirmed: false` and `confidence: null` — never `validation: null`
  - **Test:** a code returned below threshold gets `confirmed: false` with the
    real score, distinguishable from the case above
  - **Test:** a Comprehend failure stores the note with every `validation` null
  - **Test:** a transcript over 10,000 characters is chunked, not truncated, and
    a code confirmed in a later chunk is confirmed in the result
  - **Test:** CPT entries are untouched and keep `validation: null`

  - Built (236 tests, 97% coverage; 683 in track-b-rag still pass unchanged).
    **One thing here is deliberately unfinished — read the first note.**
    - **UNVERIFIED: the format of `ICD10CMConcept.Code`.** Nobody has run this
      against real AWS. There are no credentials on the development machine and
      none in CI, so points that needed live data could not be closed. ICD-10-CM
      has two standard spellings (`M17.11`, `M1711`) and botocore types the
      field as an unconstrained string, so no contract settles which one comes
      back. The exposure is confined to one pure function,
      `models.extracted_code.matching_key`, which reduces both sides of every
      comparison to a dotless key and is therefore correct under either answer —
      defensive against the unknown rather than a bet on one outcome. Closing it
      means running `scratchpad/probe_real.py` with real credentials and
      confirming that function against the response; it is not a rework of the
      validation path. `test_comprehend.py` and `test_extracted_code.py` carry
      the marker too, and the fixtures deliberately hold both spellings so the
      tests fail if anything ever starts depending on one.
    - **Three things were checked empirically rather than from documentation,
      and one of them overturned what the docs say.** Moto does not implement
      Comprehend Medical at all — `@mock_aws` returns `404 Not yet implemented`,
      because moto's `comprehend` module is Amazon Comprehend, a different
      service. The real per-request cap is **10,000 characters**, carried in
      botocore's shape metadata and enforced client-side; the 20,000-byte figure
      in general circulation belongs to `DetectEntitiesV2`, and building against
      it would have sized every chunk at twice the limit. And the response nests
      two distinct score fields whose meanings the service model separates,
      which is what settled the concept-score choice without live data.
    - **`CONFIRMATION_THRESHOLD = 0.8` is a guess, and its comment says so.** It
      came from this task's original wording, not from a measurement. No real
      distribution of concept scores has been seen, so it could be far too
      strict or far too lax. Revisit with the same live call that closes the
      format question.
    - **Validation runs before the insert and is never fatal.** The ordering is
      forced by `store_note` being `ON CONFLICT DO NOTHING` with no update path;
      the non-fatality is TASK-030's "passes fail independently" applied one
      level down, with the note as the artifact that matters and validation as
      metadata about it.
    - **`_canonicalize` was fixed, not just extended.** It previously only
      uppercased and stripped, so `M1711` and `M17.11` were two entries for one
      diagnosis and nothing anywhere reported it. That bug predates this task
      and would have made this task's output look like a finding about the codes
      rather than a formatting mismatch.
    - **The consumer takes the validator as an injected seam**, so no test in
      `test_consumer.py` can reach AWS, and `test_comprehend.py` owns the
      behaviour.
    - No new environment variables, no migration, no new route, and so no audit
      row: a code disagreeing with a second opinion is a quality metric, not a
      PHI access. `AWS_REGION` was already in `Settings` from TASK-030.
    - CI needed no change — `track-a-clinical` already has a job and no package
      was added.


- [x] **TASK-032:** SOAP note review endpoint
  - Prerequisite: TASK-030 (writes the `clinical_notes` row these routes read)
  - Service: `services/track-a-clinical`
  - **Keyed on `session_id`, not `encounter_id`.** Earlier drafts of this task
    and of TASK-071 said `/notes/{encounter_id}`, which named an identifier no
    client has ever been given: `POST /sessions/start` returns
    `{session_id, jwt}` and nothing in the service exposes `encounters.id`. See
    CLAUDE.md, "Session-scoped routes are keyed on `session_id`" — resolve the
    session to its encounter server-side, the way every other route here does.
  - `GET /notes/{session_id}` — return the generated note for provider review:
    the four SOAP sections, both code lists in the shape fixed in CLAUDE.md
    "Extracted clinical codes — one JSON shape", `generated_at`,
    `reviewed_by_provider`, `provider_edited`, and `ehr_document_ref_id`.
  - `PATCH /notes/{session_id}` — a partial update. Editable: the four SOAP
    sections, `icd10_codes`, `cpt_codes`, and `reviewed_by_provider`. Everything
    else is server-owned and rejected (`extra="forbid"`, as elsewhere).
  - **`provider_edited` is set by the server, `reviewed_by_provider` by the
    client, and neither is ever set by a read.** A `PATCH` that changes any
    content field sets `provider_edited = true`; a `PATCH` that only marks the
    note reviewed does not, because nothing was edited. `reviewed_by_provider`
    is an explicit body field: it records a provider's attestation, and inferring
    it from a `GET` would make it record that a screen was loaded instead — a
    distinction an auditor cannot recover after the fact. Nothing wrote this
    column before this task; it has no other writer.
  - **Three states on the code lists, not two.** A field the client did not send,
    a field explicitly set to `null`, and a field carrying a list are three
    different requests. Use `exclude_unset` (or an equivalent sentinel) — a
    `None` default cannot tell the first two apart, and reading an omitted field
    as `[]` would let a provider fixing a typo in the plan section silently
    declare the encounter has no diagnoses. See CLAUDE.md, "So an editing
    endpoint needs three states, not two".
  - **Accepting a Comprehend suggestion writes `source: "provider-accepted"`**,
    the third source value added to the shape contract in CLAUDE.md for this
    task. It carries no `confidence` and no `validation`, and TASK-060 may claim
    it in a bundle exactly as it claims an `llm-extraction` entry — that promotion
    is the mechanism by which a machine suggestion becomes documentation. The
    client sends the entry with the new source; the server validates the shape
    rather than inferring the transition.
  - **Two distinct not-found answers.** An unknown or soft-deleted `session_id`
    is a 404 (`session_not_found`, matching `/sessions/{id}/end`). An encounter
    that exists but has no note yet — TASK-030 still running, or its generation
    failed — is its own error code (`note_not_generated`), because a review
    screen has to say "the note isn't ready" rather than "this visit does not
    exist". Same status, different code and different wording in the spec.
  - **Both routes touch PHI and both audit**, per Known Constraints #6, with
    `READ_NOTE` and `UPDATE_NOTE` from CLAUDE.md's action vocabulary — neither
    constant exists in `audit.py` yet. `resource_type` is `ClinicalNote` and
    `resource_id` the note's id. Unlike TASK-030's consumer write, these are
    request-driven, so they carry `ip_address` and `user_agent`. The audit row
    joins the request transaction, as everywhere in this service.
  - No credential in v1, `actor_id` from `encounters.provider_id` — and
    specifically **not** `validate_remint_credential`, which 409s on completed
    encounters while note review only ever happens on one. Reasoning and the
    Phase 5 successor are in CLAUDE.md's session section; do not re-derive it.
  - No migration: every column already exists from TASK-005. Add both routes to
    `docs/api/track-a-clinical.yaml` in the same change —
    `tests/unit/api/test_openapi_contract.py` compares routes, methods, status
    codes, required fields and error codes against the running app.
  - **Test:** `GET` returns a stored note with both code lists intact
  - **Test:** `PATCH` of one SOAP section sets `provider_edited = true` and
    leaves `icd10_codes` and `cpt_codes` exactly as they were — specifically
    including a note whose `icd10_codes` is `null`, which must stay `null` and
    not become `[]`
  - **Test:** `PATCH` with `icd10_codes: null` explicitly clears the column, and
    is distinguishable from the omitted case above
  - **Test:** `PATCH` setting only `reviewed_by_provider` leaves
    `provider_edited` false
  - **Test:** `GET` never sets `reviewed_by_provider`
  - **Test:** a `comprehend-medical` entry re-sent as `provider-accepted` is
    stored as such, and one re-sent as `provider-accepted` with a `confidence`
    is rejected
  - **Test:** unknown `session_id` → 404 `session_not_found`; encounter with no
    note → 404 `note_not_generated`
  - **Test:** both routes write their audit row on the request's transaction
  - Built (319 tests, 100% coverage). Decisions worth knowing before touching this:
    - **Four of this task's decisions are in CLAUDE.md, not here**, because each
      binds another task: the `session_id` keying and the v1 no-credential rule
      are in the Session Lifecycle section, and the `provider-accepted` source
      and the three-state PATCH are in the shape contract. TASK-060 and TASK-071
      both depend on them. Read them there rather than inferring from the code.
    - **The tri-state is implemented as `model_fields_set`, not as a `None`
      default.** `UpdateNoteRequest.edited_fields()` returns only the keys the
      request actually carried, so an omitted field never reaches the update at
      all. A `None` default cannot express this: it makes "omitted" and
      "explicitly null" the same value, and the direction that fails is the
      dangerous one — a text edit silently clearing an encounter's diagnoses.
      `tests/integration/test_notes_api.py` proves it through the JSONB column
      rather than through an in-memory object, which is the only place a NULL
      and a `[]` are genuinely distinguishable.
    - **`provider_edited` is set on a real change, not on the arrival of a
      PATCH.** A client re-sending the text it was given has edited nothing, and
      a flag that says otherwise stops meaning anything. It is never cleared: a
      note that was edited stays edited even if the original wording is restored.
    - **A PATCH audits even when nothing changed.** The row was opened on a
      request, which is the access an audit is asked about. Only the 404 paths
      audit nothing — there was no note to access.
    - **`ExtractedCode` gained a second model validator rather than a branch in
      the existing one.** `provider-accepted` refuses a `validation` outright,
      which is its own rule; it shares the no-confidence check with
      `llm-extraction` because that rule is identical even though the arguments
      for the two are opposite — a model's self-rating is not a measurement, a
      human's decision is not a probability. The error message names whichever
      applies.
    - **The route tests use a two-step fake session** that answers the encounter
      query and then the note query in order. That mirrors
      `_load_encounter_and_note` and would need revisiting if the handler ever
      loads them in one statement.
    - No migration, no new environment variable, no new Redis key, and no CI
      change — `track-a-clinical` already has a job, and the spec edit selects it
      by the `docs/api/<service-name>.yaml` convention.
    - Deferred deliberately: no optimistic concurrency on the PATCH. Two
      providers editing one note concurrently would have the second overwrite
      the first, silently. Nothing in the product puts two people on one note
      today, and the fix is an `If-Match`/version column that belongs with real
      provider authentication in Phase 5 rather than ahead of it.

---

## Phase 4 — Live Nudge System

- [x] **TASK-040:** Nudge emitter
  - Prerequisite: TASK-012 (the answer this turns into a nudge), TASK-021 (the
    consumer that holds that answer today and discards it), TASK-024 (the
    procedure key the dedup claim is held on), TASK-005 (`clinical_nudges`)
  - Service: `services/track-b-rag`
  - **The payload shape is fixed in CLAUDE.md, "The nudge payload — one shape",
    not here.** TASK-041 relays it, TASK-042 and TASK-043 render it and TASK-044
    publishes a second variety of it; four readers and one writer is a contract
    that gets settled once. This task implements that shape and does not
    redefine it. Note `cpt_code` is nullable there from the start even though
    nothing this task emits sets it to null — TASK-044 is why.
  - **Ship the trigger fix first, as its own commit.** CLAUDE.md's "The nudge
    trigger is the message" corrects logic that shipped with TASK-012:
    `gap_analysis` composes a real provider-facing message in two cases the
    original `missing_criteria`/`denial_risk` trigger never fires on, so the
    message is written and never shown. The fix makes `nudge_message()` return
    `str | None`, gives that decision exactly one home, and makes
    `PolicyQueryData.nudge_message` nullable (`docs/api/track-b-rag.yaml`
    changes with it, and the contract drift test guards the pair). It is a
    correctness fix to already-shipped code, so it lands as a labelled `fix(...)`
    commit before any of this task's new code and carries its own regression
    tests for both previously silent cases. Do not fold it into the feature
    commit — a bisect should be able to land between them.
  - **Emit from the consumer, not from the route.** `transcript_consumer` is
    what knows a procedure was ordered during a live encounter;
    `POST /policies/query` is a query endpoint, and making it write rows and
    publish as a side effect hands that behaviour to every future caller of it.
    The seam is `transcript_consumer._query_for`, which already receives the
    answer from `policy_dispatch.resolve_and_query_policy()` and throws it away.
  - **The route's answer must carry `source`, and the emitter must not
    reconstruct it.** `query.PolicyQueryAnswer.source` distinguishes `cache`,
    `rag`, `crd*` and `fallback`, and the HTTP response drops it, so a consumer
    cannot currently tell a real answer from the safe fallback. Add `source` to
    `PolicyQueryData` (and to the OpenAPI spec) rather than inferring
    "this looks like a fallback" from the risk level and an empty criteria list
    — that inference is a fourth derivation of a decision this PR is busy
    reducing to one.
  - **`haptic` is not `denial_risk == "high"`.** It is that *and* a non-fallback
    answer. The full reasoning — an outage must not spend the credibility of the
    channel that genuine high-risk nudges depend on — is in CLAUDE.md with the
    payload shape. State it in the emitter as an explicit rule with the reason
    attached, never as a default inherited from the risk field.
  - **`encounter_id` needs one more column on an existing SELECT.**
    `clinical_nudges.encounter_id` is a FK to `encounters.id`, and the consumer
    holds only `session_id`. Add `Encounter.id` to the statement in
    `policy_dispatch.resolve_query_parameters()` and carry it on
    `PolicyQueryParameters`. That SELECT is guarded by TASK-024 in two places,
    because adding a patient column there would silently turn a non-PHI read
    into a PHI one, and **both are updated deliberately as part of this
    change** rather than being allowed to pass by coincidence:
    - `_Row` in `tests/unit/test_policy_dispatch.py` is a fake carrying exactly
      the four columns the real SELECT asks for, and deliberately *not* an
      `Encounter`, so a SELECT that had started reading PHI would have nothing
      to read it from. Adding `id` breaks it with an `AttributeError` — that is
      the guard doing its job; add the column to the fake.
    - The integration test in `tests/integration/test_query_parameters.py`
      compiles the emitted SQL and asserts `patient_fhir_id`,
      `insurance_member_id` and `ehr_encounter_id` are absent. Those assertions
      do not change — add one naming `id` as now expected, so the column list
      stays pinned in both directions rather than only against additions.
    `id` is a row identifier and not a patient attribute, so the compliance
    answer the guard protects is unchanged.
  - **Writing the nudge is a PHI write and audits as `WRITE_NUDGE`**, added to
    CLAUDE.md's action vocabulary in this PR — along with `QUERY_POLICY`, which
    TASK-012 shipped in code without ever adding to that list. `resource_type`
    is `ClinicalNudge`, `resource_id` the new row's id. Per CLAUDE.md's
    "Auditing work that no request triggered": `actor_id` is
    `encounters.provider_id` — never a minted service-account UUID — `session_id`
    comes from the same row, and `ip_address`/`user_agent` are permanently
    `None` here rather than pending, because a Redis-driven consumer has no
    client and never will.
  - **The audit row joins the insert's transaction**, which needs a
    `raw_asyncpg_connection()` helper in track-b-rag's `db.py` mirroring
    track-a-clinical's. Checked why it was omitted: `db.py` was written in
    TASK-011, when this service's only route was `/policies/ingest` and wrote no
    audit row at all, and it has not been touched since. TASK-012 then added an
    audited route that writes no domain row, so nothing forced the helper then
    either. So this is a clean gap-close and not a reversal — but note the
    module docstring's claim that "this service's one route writes no audit row
    at all" went stale the moment TASK-012 landed, and correct it in the same
    change.
  - **Thread `policy_id` through Stage 1 to fill `payer_policy_source`.** The
    column is described on the model as "the one field that makes a nudge
    auditable after the fact", and nothing supplies it: `RetrievedChunk` carries
    `policy_id` and it is dropped before `PolicyRules` is built. Carry it onto
    the Stage 1 result so the emitter can record which policy the criteria came
    from. It is payer-scoped, not patient-scoped, so it caches under the
    existing `rag:` key without violating the two-stage split — and make the
    field optional so entries cached before this change still deserialise
    instead of forcing a mass fallback on deploy.
  - **Store, then publish, both inside the dedup claim.** The row is written
    first because the payload carries its `nudge_id`; a client must not be able
    to acknowledge a nudge that was never recorded. The claim from
    `dedup.claim_procedure()` is what makes one order raise one nudge, so the
    emit happens inside the region it protects.
  - **A publish failure must not become a duplicate row.** `_query_for` releases
    the claim on a transient failure so a later mention gets another attempt —
    correct for a failed query, wrong for a stored-but-unpublished nudge, which
    would re-insert on the retry. The retry path checks for an existing nudge
    for that encounter and procedure before inserting, and republishes the row
    it finds rather than writing a second one. Same reasoning as TASK-011's
    write ordering: structure the sequence so a mid-operation failure is
    recoverable rather than silently duplicative.
  - Enforce that in the schema too, not only in the retry path: a partial unique
    index on `(encounter_id, cpt_code) WHERE cpt_code IS NOT NULL`, as a real
    Alembic migration in track-a-clinical (`alembic_version_track_a_clinical`).
    "One nudge per procedure per encounter" is already assumed by the dedup
    guard and by TASK-041b's acknowledge; an assumed invariant that holds only
    because one code path remembers to check is one bad retry away from being
    false. The `WHERE` clause is deliberate — TASK-044's keyword-only nudges
    have a null `cpt_code` and NULLs do not collide in a unique index, so that
    half of the invariant lands with the task that introduces the case.
  - **This does not run end to end yet, and TASK-052b is the gate.**
    `resolve_query_parameters()` still raises `MissingQueryParameters` for every
    real encounter until the payer columns are populated at SMART launch, so no
    live transcript reaches this emitter. Test against a directly constructed
    answer rather than inventing placeholder payer values — the reasoning that
    ruled those out in TASK-021 and TASK-024 is unchanged and is stronger here,
    since a nudge is what a provider actually sees. Say so plainly in the build
    notes rather than implying the path is live.
  - **Test:** an answer with known missing criteria — verify the Redis publish
    carries a `nudge_id` matching the stored row
  - **Test:** a `fallback` answer publishes a nudge with `denial_risk == "high"`
    and `haptic` false
  - **Test:** an answer with nothing worth saying (`nudge_message is None`)
    stores no row and publishes nothing
  - **Test:** a publish failure after the row is stored does not produce a
    second row when the same procedure is mentioned again
  - **Test:** the `WRITE_NUDGE` audit row names the encounter's `provider_id` as
    actor and carries no procedure, code or criteria text
  - Built (725 tests in track-b-rag, 321 in track-a-clinical, 100% coverage on
    both). Shipped as six commits, the first of them a labelled bugfix against
    TASK-012 rather than part of this feature. Decisions worth knowing before
    touching this:
    - **`gap_analysis.nudge_message()` returns `str | None` and that is the
      trigger.** The two silent cases are fixed and `PolicyQueryData.nudge_message`
      is nullable to carry the decision over HTTP. Worth knowing if this is ever
      revisited: the obvious repair — triggering on a non-empty message while
      leaving the function as it was — nudges on *every* query, because every
      branch returned prose including "No prior authorization required". The
      message had to be allowed to be absent before it could be a signal.
    - **`nudges.should_escalate()` is the only place `haptic` is decided**, and
      the reason is written at the decision rather than in a task file. A client
      that re-derives the buzz from `denial_risk` reinstates exactly what the
      rule prevents, so TASK-043's bullet says so too.
    - **`PolicyQueryData` gained `source` and `policy_source`.** The first
      reverses a standing decision, narrowly: the response deliberately said
      nothing about which path answered, which still holds for cache/rag/crd and
      is stated in the field's own description — but `fallback` is the difference
      between an answer and the absence of one, and the emitter needs it. The
      second fills `clinical_nudges.payer_policy_source`, which no code path
      could supply before.
    - **`policy_source` is overwritten from the retrieved chunks, always.** It
      is a field on the model the LLM's answer is parsed into, so an answer
      naming its own sources would otherwise be believed and cached. It is the
      string a reviewer checks a nudge's criteria against; a plausible
      fabrication there is worse than an empty column.
    - **The dedup claim now protects the store and the publish**, and migration
      0005 is what makes a retry safe rather than duplicating. The emitter
      raises rather than swallowing, because the consumer releasing the claim is
      how the next mention gets another attempt — a swallowed failure would
      silence that procedure for the rest of the visit.
    - **A republish writes no second audit row.** The nudge was raised once and
      recorded once; the retry is re-sending something already recorded. One row
      per unit of work, as CLAUDE.md's consumer-auditing section has it.
    - **`db.raw_asyncpg_connection()` was a clean gap-close, not a reversal.**
      It was absent because TASK-011 wrote that module when the service's only
      route audited nothing; TASK-012's audited route writes no domain row, so
      nothing forced it then either. The module docstring's claim that this
      service audits nothing had been stale since TASK-012 and is corrected.
    - **CI had a hole this branch would have fallen into.** A change to
      track-a-clinical's shared models selected no job in track-b-rag or
      policy-scraper, both of which import them. Fixed in
      `detect-changed-members.sh`, scoped to `src/` so a migration still selects
      only the JWT pairing, with a case for each direction.
    - **Still not reachable end to end.** `resolve_query_parameters()` raises for
      every real encounter until TASK-052b populates the payer columns, so no
      live transcript reaches the emitter. Everything here is tested against a
      directly constructed answer, and the store against a real database.
    - Deferred deliberately: keyword-only nudges are TASK-044's, and so is the
      other half of the uniqueness invariant — the partial index does not
      constrain rows with a null `cpt_code`, which is pinned as a test rather
      than left to be discovered.

- [x] **TASK-041:** Nudge WebSocket relay
  - Prerequisite: TASK-006 (JWT), same auth pattern as TASK-020
  - Service: `services/nudge-service`
  - `WebSocket /ws/nudges/{session_id}` — same JWT validation as TASK-020
    (verify signature, exp, session_id claim match; 4401 on failure), and the
    same two token carriers: `Authorization: Bearer` or the
    `Sec-WebSocket-Protocol` entry. See CLAUDE.md, "How the JWT reaches a
    WebSocket endpoint" — `apps/web` opens this socket too and can only use the
    subprotocol form.
  - **"Same as TASK-020" is an extraction, not a second implementation.** That
    validation already exists, in `services/audio-ingestion/src/auth.py`, and
    this task is its second real consumer — the exact condition that produced
    `packages/api-envelope` in TASK-010, where track-b-rag's copy of
    track-a-clinical's envelope was extracted rather than maintained twice.
    Known Constraint 8 forbids a parallel session-auth mechanism, and two
    hand-maintained copies of one validator is how a parallel mechanism arrives
    without anyone deciding to build one. So:
    - Extract the carriers, the token validation and the 4401 close code into
      **`packages/session-auth`**, in this task, before the relay is written.
    - Migrate `audio-ingestion` onto the package in the same PR. Its existing
      suite is the regression evidence that the extraction preserved behaviour,
      so it must pass unchanged apart from import paths — do not adjust an
      assertion to accommodate the move.
    - The package gets its own path-filter entry and CI job, per the packages
      rule in CLAUDE.md's ci.yml section. A package without its own job is the
      silent hole that rule exists to close.
    - The package validates and never mints. `POST /sessions/start` stays the
      only issuer, per Known Constraint 8.
  - Subscribe to `nudges:{session_id}`
  - Forward each nudge event to connected client in real time — verbatim. The
    relay does not reshape, filter or enrich the payload; CLAUDE.md's "The nudge
    payload — one shape" is the contract, and a relay that edits it becomes a
    second definition of it.
  - **Relay the raw payload string; do not deserialize or model it here.**
    Parsing to JSON and re-serializing would satisfy the letter of "verbatim"
    while reintroducing what it forbids — a Pydantic model of the nudge in this
    service is a second definition of TASK-040's shape, free to drift from the
    one that writes it. The relay needs to know the channel name, not the
    message.
  - On client disconnect: unsubscribe from Redis channel
  - **The relay audits, as `RELAY_NUDGES`.** A nudge names a procedure and the
    payer criteria still undocumented for an identified encounter, so opening
    the stream is a PHI access by the Known Constraint 6 test, and this is the
    only record that the access happened. One row per accepted connection, not
    per relayed message — the same judgement TASK-020 made for a connection that
    carries hundreds of segments. A refused connection writes none: no PHI was
    reached, and it logs at WARNING instead. `actor_id` is the `provider_id`
    claim from the validated token; this service reads no tables.
  - `GET /health` reports Redis, fatal rather than advisory: a relay that cannot
    reach the bus delivers nothing, and a silent visit is indistinguishable from
    a visit with nothing to flag. Unaudited, per Known Constraint 6.
  - `docs/api/nudge-service.yaml` with a drift test, following
    `docs/api/audio-ingestion.yaml` — the WebSocket goes under
    `x-websocket-endpoints` because OpenAPI 3.1 cannot describe one.
  - **Test:** publish nudge to Redis, verify it appears at WebSocket client
  - **Test:** connect with invalid JWT, verify 4401 close
  - **Test:** a payload the relay cannot parse still reaches the client
    unaltered — the property "verbatim" actually means
  - **Test:** audio-ingestion's suite passes on the extracted package
  - Built (74 tests, 100% coverage; packages/session-auth 35 tests, 100%).
    Decisions worth knowing before touching this:
    - **The extraction came first and audio-ingestion moved onto it in the same
      PR**, so there was never a moment with two copies of the validator. Its
      suite is the evidence the move preserved behaviour: no assertion changed,
      only imports and two call shapes. `validate_token()` now takes a signing
      key rather than a `Settings` object, so no service's configuration class
      is part of the package's interface.
    - **The issuer keeps its own `JWT_ALGORITHM` and `MIN_SIGNING_KEY_BYTES`,
      and that is deliberate.** track-a-clinical is not a consumer of this
      validator, and making it import the package would invert what Known
      Constraint 8 centralises. The contract test moved into the package and
      now also asserts the two key floors match — a stronger check than a shared
      literal, which would prove agreement on an algorithm name while saying
      nothing about the claim set.
    - **The payload is never deserialized here.** A test asserts on the exact
      string rather than on decoded JSON, because a relay that parsed and
      re-serialized would pass an equality check on the decoded object while
      quietly becoming a second definition of the shape. That also means this
      service needs no change for TASK-044's keyword-only nudge.
    - **A payload the relay cannot parse is still relayed.** Only a non-UTF-8
      one is dropped, because a text frame cannot carry it. Dropping malformed
      messages would turn an emitter bug into silence at the bedside, which a
      provider cannot distinguish from nothing to flag.
    - **Inbound frames are ignored rather than closing the socket**, unlike the
      audio socket's 1003. Nothing travels client-to-server here, so a keepalive
      is not a broken client. The receive loop exists only to notice the
      disconnect.
    - **1011 is a close code a client actually observes**, unlike 4401. It is
      sent when the subscription fails *after* the handshake was accepted, which
      is also the one case where an audit row exists for a stream that relayed
      nothing — the access had already happened.
    - **CI had the same hole one directory over.** The selection suite pinned
      `ALL_SERVICES` against the real tree but not `ALL_PACKAGES`, so a package
      missing from that array would silently get no test job — how
      `packages/hipaa-logger` went untested before. Added, keyed on the presence
      of a `pyproject.toml` so TypeScript-only `packages/audio-wire` is
      correctly excluded.
    - **Still not reachable end to end.** Nothing publishes to
      `nudges:{session_id}` for a real encounter until TASK-052b supplies the
      payer columns TASK-040's emitter needs, so the integration suite publishes
      to Redis directly. What it proves is that a real publisher on the
      canonical channel reaches a real subscriber here.
    - Deferred deliberately: `PATCH /nudges/{nudge_id}/acknowledge` is
      TASK-041b's and lives in track-b-rag, which owns the `clinical_nudges`
      write. The vocabulary-drift test this task's audit action prompted is
      TASK-045, kept out of here because it tests every service against a root
      document and has nothing to do with relaying nudges.

- [ ] **TASK-041b:** Nudge acknowledge endpoint
  - Service: `services/track-b-rag` (owns the clinical_nudges write from TASK-040)
  - Prerequisite: TASK-040, which writes the row and puts its `nudge_id` in the
    published payload. Blocks TASK-042 and TASK-043, whose dismiss buttons call
    this. Not actually reachable from `apps/web` until TASK-041c settles CORS.
  - `PATCH /nudges/{nudge_id}/acknowledge` — sets `acknowledged=true`,
    `acknowledged_at=NOW()`. This is what TASK-042's dismiss button calls —
    it was referenced by the UI task but never specified as its own endpoint
    until now.
  - **The body is `{"acknowledged": true}`, not an empty PATCH — and that is
    the default shape for a PATCH-as-state-transition route in this repository,
    not a decision local to this endpoint.** A transition carrying no parameters
    could take no body at all, and the call is genuinely marginal. The explicit
    field wins on three small things at once: it satisfies Known Constraints #6's
    request-model requirement in spirit rather than by argument, it makes the
    request self-describing in `docs/api/track-b-rag.yaml` instead of an empty
    box a reader has to infer meaning for, and it leaves room to extend the
    endpoint later — a reason code, say — without turning an empty body into a
    populated one, which is a breaking change for anyone who hardcoded the
    former. Reject `false` rather than reading it as an un-acknowledge: this
    endpoint sets the flag, the reverse transition is not specified, and
    inventing it here would put a compliance-relevant flag under a caller's
    control in both directions.
  - **Repeat acknowledge copies TASK-006's idempotent `/sessions/{id}/end`
    exactly.** 200, the *original* `acknowledged_at` rather than a fresh one,
    and an `already_acknowledged` flag telling the client which happened — the
    counterpart of that endpoint's `already_ended`. The audit action reflects
    whether the row actually changed: `ACKNOWLEDGE_NUDGE` when it did,
    `READ_NUDGE` when it did not, which is the same distinction that makes an
    idempotent session-end audit as `READ_ENCOUNTER` rather than `END_SESSION`.
    Copy that precedent rather than re-deriving a near-identical variant of it:
    two endpoints answering the same question in slightly different shapes is
    worse for a client than either shape alone.
  - **`ACKNOWLEDGE_NUDGE` is a new action and goes into CLAUDE.md's vocabulary
    table in the same PR**, per that table's own rule; `READ_NUDGE` already
    exists and its "Written by" column gains track-b-rag alongside prior-auth.
    TASK-045's drift test is not built yet, so nothing mechanical catches this
    being skipped — it would be the fourth instance of exactly the drift that
    task exists to stop.
  - **404 for an unknown `nudge_id`, and 404 for a nudge whose encounter is
    soft-deleted.** `clinical_nudges` has no `deleted_at` of its own —
    deliberately, since a nudge records what a provider was told at a point in
    time — so the second case is visible *only* through an explicit join to
    `encounters.deleted_at`. An implementation that checks the nudge row alone
    passes every other test here while leaving a retired encounter's nudges
    mutable, which the session routes' "unknown or soft-deleted → 404" rule
    already forbids one table over. Perform the join; test that case by name.
  - **The route touches PHI and audits.** A nudge names a procedure and the
    payer criteria an identified encounter has not documented — the same
    judgement TASK-041 made in deciding that opening the relay is a PHI access.
    `actor_id` is `encounters.provider_id`, reached through the join above and
    never taken from the caller.
  - **No credential in v1; the audit row is what stands in for one.** See
    CLAUDE.md, "A route keyed on a resource rather than a session follows the
    same v1 rule", which is where this was settled because TASK-042 and TASK-043
    hit it identically. That section also records why `packages/session-auth`
    does not fit a path with no `session_id` segment, and what the eventual fix
    (validating against a *resolved* session id) looks like. Do not invent a
    local check here — Known Constraints #8.
  - **The audit write joins the route's transaction.** This route mutates a
    domain row, unlike `/policies/query`, so it is `audit_nudge_write`'s case
    rather than `audit_policy_query`'s: `db.raw_asyncpg_connection(session)` and
    `audit_log(..., conn=...)`. An acknowledged nudge with no audit row, and an
    audit row for an acknowledgement that rolled back, are both worse than the
    write failing outright.
  - New router module under `src/track_b_rag/api/` — this is the service's first
    route outside the `/policies` prefix. Standard envelope from
    `packages/api-envelope`, with `error_responses()` carrying 404 wording for
    this route rather than the generic per-status default.
  - `docs/api/track-b-rag.yaml` gains the route in the same change;
    `tests/unit/api/test_openapi_contract.py` compares the committed spec
    against the generated schema, and a spec-only edit selects this service's
    CI job.
  - **Test:** acknowledge a nudge, verify row updated
  - **Test:** acknowledge twice — the second call returns 200 with
    `already_acknowledged` true and the original `acknowledged_at` unchanged
  - **Test:** the first call audits as `ACKNOWLEDGE_NUDGE`, the second as
    `READ_NUDGE`
  - **Test:** unknown `nudge_id` returns 404
  - **Test:** a nudge whose encounter is soft-deleted returns 404 and leaves the
    row unchanged
  - **Test:** the audit row names the encounter's `provider_id` as actor and
    carries no procedure, code or criteria text
  - **Test:** `{"acknowledged": false}` is rejected rather than un-acknowledging

- [ ] **TASK-041c:** CORS policy for browser-facing routes
  - Prerequisite: none. Blocks TASK-042 and every browser-facing route after it.
  - **The gap.** No service in this repository installs `CORSMiddleware`, and
    nothing in front of them supplies it either — there is no ingress or gateway
    in the tree. That has been fine because every route built so far is
    service-to-service, documented route by route as "access control is network
    isolation only". TASK-042 is the first browser caller: `apps/web` dismissing
    a nudge issues a cross-origin `PATCH` with a JSON content type, which
    triggers a preflight, and with no CORS answer the browser refuses the
    request before the service ever sees it. Found while specifying TASK-041b
    and opened as its own task rather than solved inside it — one service
    quietly growing a permissive middleware is how a repo-wide policy gets set
    by accident.
  - **`apps/mobile` is unaffected**, because React Native's fetch is not subject
    to the same-origin policy. That is why this is deliberate rather than
    urgent — and why TASK-043 working on a phone proves nothing about TASK-042
    working in a browser.
  - **Decide where the policy lives, once:** per-service `CORSMiddleware`, or a
    single ingress in front of every service. Both are defensible; choosing
    per service is not.
  - Allowed origins come from configuration per environment — never a hardcoded
    literal, and never `*` on a service that answers with PHI. The allowed
    methods and headers, and whether credentials are permitted, are part of the
    same decision rather than left to each route.
  - **WebSocket handshakes are a separate question, settled at the same time.**
    Browsers do not apply CORS to a WebSocket upgrade, so the nudge socket
    (TASK-041) and the audio socket (TASK-020) are unaffected by whatever is
    chosen here. The corollary is that nothing checks their `Origin` today, and
    assuming CORS covered them would be wrong — decide it while the subject is
    open.
  - **Test:** a preflight `OPTIONS` from a configured origin is answered with
    the route's methods
  - **Test:** an origin outside the configured list is not granted
  - **Test:** the allowed origins come from configuration, not a literal

- [ ] **TASK-042:** Nudge UI component (web)
  - App: `apps/web`
  - `<NudgeOverlay sessionId={...} />` — subscribes to nudge WebSocket
  - Payload shape: CLAUDE.md, "The nudge payload — one shape". Note `cpt_code`
    is nullable there — TASK-044 emits nudges without one, so the component must
    render a nudge that names no code rather than assuming a string.
  - High-contrast banner, color-coded by denial_risk (yellow/orange/red)
  - Dismiss button calls `PATCH /nudges/{nudge_id}/acknowledge` (TASK-041b)
    using the `nudge_id` included in the WebSocket payload
  - **That call carries no credential in v1** — CLAUDE.md, "A route keyed on a
    resource rather than a session follows the same v1 rule". Do not attach the
    session JWT to it on the assumption that a PHI-touching route must want one;
    the endpoint does not validate it, and sending a bearer token to a route
    that ignores it is how a client comes to believe it is authenticated.
  - **Blocked on TASK-041c.** The dismiss is cross-origin and preflighted, and
    no service in this repository answers CORS today, so this component cannot
    be verified end to end against a running service until that lands. The
    WebSocket subscription is unaffected — browsers do not apply CORS to a
    WebSocket upgrade — so the alert half of this task can be built first.
  - Accessible (ARIA role=alert, focus management)
  - **Test:** render with mock WebSocket, verify alert appears on message
  - **Test:** click dismiss, verify acknowledge endpoint is called with correct nudge_id

- [ ] **TASK-043:** Haptic nudge (mobile)
  - App: `apps/mobile`
  - On nudge received with `haptic: true`: call `Haptics.notificationAsync()`.
    Trigger on that field alone — never on `denial_risk == "high"`. The emitter
    deliberately withholds `haptic` on a high-risk answer it could not verify
    (CLAUDE.md, "The nudge payload — one shape"), and a client that re-derives
    the buzz from the risk level reinstates exactly the behaviour that rule
    exists to prevent: an infrastructure outage buzzing a physician's device
    once per procedure until they stop trusting the alert.
  - Same visual alert as web, same dismiss-calls-acknowledge behavior (TASK-041b),
    and the same absence of a credential on that call — CLAUDE.md, "A route keyed
    on a resource rather than a session follows the same v1 rule". Both clients
    cite that section rather than each deciding it, which is why it is written
    there and not in either task.
  - Unlike TASK-042 this is not blocked on TASK-041c: React Native's fetch is
    not subject to the same-origin policy, so no preflight is involved. Do not
    read a working dismiss here as evidence that the web dismiss works.
  - **Test:** mock WebSocket message, verify Haptics mock called

- [ ] **TASK-044:** Keyword-only nudge for procedures that resolve no CPT code
  - Prerequisite: TASK-024 (the resolver and its four refusal reasons),
    TASK-040 (the nudge emitter), TASK-052b (nothing nudges anyone before it)
  - Service: `services/track-b-rag`
  - **The gap.** TASK-024 maps eight of the ten detector keywords to CPT codes.
    X-ray and biopsy resolve nothing and, as of that task, produce complete
    silence: the keyword is detected, one WARNING is logged, no policy query is
    made and the provider sees nothing at all. The same silence covers an
    arthroscopy or injection whose qualifier was never spoken — "she may need an
    arthroscopy" — and an epidural or trigger point injection, which are
    recognised and deliberately uncoded.
  - **What this would add.** A nudge driven by the keyword alone: "an
    arthroscopy usually requires prior authorization with this payer — check
    before ordering." It could not name missing criteria, because those come out
    of the RAG path and the RAG path needs a code to filter on. It would not be
    empty either.
  - **The payload already has room for this and needs no new shape.**
    CLAUDE.md's "The nudge payload — one shape" makes `cpt_code` nullable
    precisely for this task, and TASK-042 is required to render a nudge that
    names no code. Two things this task still owes, because TASK-040 scoped them
    to the coded case: the `procedure_seen:` claim is held on `keyword:{keyword}`
    rather than `cpt:{code}`, and the partial unique index TASK-040 adds on
    `(encounter_id, cpt_code)` does not constrain rows whose `cpt_code` is null —
    NULLs do not collide — so the once-per-encounter invariant for keyword-only
    nudges has to be enforced here rather than assumed to be inherited.
  - **Decide first whether this clears the dismiss-fatigue bar, because the
    repository has already argued the other way once.** CLAUDE.md rejects a
    CRD-only answer on the grounds that it "would hand Stage 2 an empty criteria
    list and a nudge that cannot say what is missing, which is most of the
    product". A banner that cannot say what to do trains providers to dismiss
    banners without reading them, and that cost lands on the nudges that *do*
    have something to say. This task is worth doing only if the answer is that a
    bare "check this" beats silence for a procedure we cannot code — and that is
    a question for a clinician, not an engineer.
  - **If it goes ahead, the four refusal reasons are the routing table**, and
    they are not all equal:
    - `REASON_AXIS_NOT_SPOKEN` (X-ray, biopsy) — permanent, and the two differ
      in value: an X-ray is rarely gated at all, so nudging on one is close to
      pure noise, while a breast biopsy genuinely is gated.
    - `REASON_QUALIFIER_NOT_STATED` (a bare arthroscopy or injection) — the
      likeliest candidate, since these are gated procedures and the only thing
      missing is a word.
    - `REASON_QUALIFIER_UNMAPPED` (an epidural, a TEE) — should raise a
      table-extension signal for us rather than a nudge for the provider.
    - `REASON_NO_CPT_EXISTS` (a biologic, a referral) — never nudge. A referral
      has no procedure authorization to check.
  - **Whatever it emits is not a policy answer and must not read like one.** No
    `requires_auth`, no `denial_risk`, no cached entry — there is no CPT code, so
    there is no `rag:` key to write under and nothing that could be cached
    without inventing one. That is the same constraint TASK-024 was built around.
  - **Test:** a bare "she may need an arthroscopy" produces the keyword-only
    nudge and no policy query
  - **Test:** a biologic or a referral produces no nudge of any kind
  - **Test:** nothing on this path writes a `rag:` cache entry

- [ ] **TASK-045:** Test that the audit action vocabulary matches the code
  - Prerequisite: none. Small, and deliberately not folded into TASK-041, which
    is where the need was noticed.
  - **Why.** CLAUDE.md's action vocabulary table is declared authoritative and
    has now drifted from the code three times, in both directions: `WRITE_NOTE`
    was cited by a task while no service defined it, `QUERY_POLICY` shipped in
    `track_b_rag/audit.py` while the list had never carried it, and
    `STREAM_AUDIO` did the same from `audio-ingestion/src/audit.py`. Each was
    found by someone working on something else. Three times is enough to say
    that noticing is not a control.
  - **What to build.** A test that collects every action constant the services
    actually declare — the `ACTION_*` names in each service's `audit.py` —
    parses the action column out of the table in CLAUDE.md, and asserts the two
    agree. Both directions: a constant missing from the table fails, and a table
    row naming a service that declares no such constant fails.
  - **The reverse direction needs a carve-out and it should be explicit.** The
    table legitimately carries actions for work not yet built — `READ_PATIENT`
    (Phase 5), `SUBMIT_PRIOR_AUTH` (TASK-061). Those rows have no constant and
    must not fail the test. Key the exemption on the row's own "Written by"
    column naming a service that has no `audit.py` yet, rather than on a
    hand-maintained ignore list, which would be one more thing to drift.
  - **Where it lives.** It tests a document at the repository root against every
    service, so it belongs to no one service. Put it under `tests/` at the root
    with its own CI job, selected by a change to `CLAUDE.md` or to any
    `**/audit.py`.
  - **A row can name more than one service, and the exemption is per service.**
    `READ_NUDGE`'s "Written by" column names prior-auth (TASK-060, unbuilt) and
    track-b-rag (TASK-041b) as of that task, so a parser treating the column as
    a single service name either exempts the whole row — losing the check on the
    half that is built — or fails on the half that is not. Split the column and
    apply the carve-out to each service named in it.
  - **Note what this does not do.** It cannot check that a row's *meaning* is
    right, or that a service audits when it should — Known Constraint 6 is still
    a judgement. It checks that the two lists of names agree, which is the part
    that has actually failed three times.
  - **Test:** a service constant absent from the table fails the check
  - **Test:** a table row for a service that has an `audit.py` without that
    constant fails the check
  - **Test:** a row for an unbuilt service passes

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

- [ ] **TASK-052b:** Populate encounter payer, plan type and state at SMART launch
  - Prerequisite: **TASK-051** (the SMART launch that produces a session),
    **TASK-052** (`get_coverage()`, which is what actually reads the payer off a
    FHIR `Coverage` resource), TASK-006 (owns `POST /sessions/start`, which is
    where an encounter row is created), TASK-024 (added the `state` column and
    the resolution that consumes these three)
  - Services: `services/fhir-integration`, `services/track-a-clinical`
  - **Why this exists as its own task.** TASK-024 closed three of the four
    parameters `POST /policies/query` needs — `cpt_code` from the keyword
    mapping, `provider_id` from the encounter row — and could not close
    `payer`, `plan_type` or `state`, because the only honest source for them is
    a FHIR `Coverage` resource fetched at SMART launch and neither TASK-051 nor
    TASK-052 exists. TASK-024 shipped without them rather than waiting, and
    without inventing a placeholder: `rag:{payer}:{plan_type}:{state}:{cpt_code}`
    is a cache key, so a fabricated payer or plan writes a real policy answer
    under a key standing for a different plan and serves it to the next
    encounter. Silent, and it crosses patients — the same bug class TASK-016
    and TASK-017 already fixed once for the payer slug.
  - **This is the task that makes Track B run end to end.** Until it lands,
    `resolve_query_parameters()` raises for every real encounter, the transcript
    consumer logs "no source yet for payer, plan_type, state" once per procedure
    per session, and no nudge can ever fire. Nothing else is missing.
  - Populate the three columns on the `encounters` row:
    - `insurance_payer` — the payer's own display name from `Coverage.payor`,
      kept as spelled. It is normalised to a slug by `/policies/query` through
      `payer_vocab.normalize_payer()`, which is the single normalisation site;
      do not slug it on the way in as well, and do not store a slug in a column
      the schema documents as the payer's own spelling.
    - `insurance_plan_type` — from `Coverage.type` / `Coverage.class`. Decide
      deliberately what to do when the resource carries neither, and write it
      down; a plan type guessed from the payer's name is a fabricated cache key
      segment.
    - `state` — two-character USPS, through `payer_vocab.normalize_state()`, so
      it speaks the vocabulary `insurance_policies.state` is matched against.
      Decide where it comes from: the patient's address, the practice location,
      or the `Coverage` resource — they disagree for a patient treated out of
      state, and which one the payer's policy follows is the question to answer.
  - **A partial `Coverage` is not an error and must not become a guess.**
    TASK-052 already returns `requires_manual_confirmation: true` rather than
    failing when payer info is incomplete. A column left NULL here is correct
    and the resolution seam already handles it: it names exactly the fields
    still absent. Filling one in with a default would be worse than leaving it.
  - **Before closing this task, the CPT table must be clinically reviewed.**
    TASK-024's `procedure_codes.py` was written from general knowledge, not from
    a licensed AMA CPT distribution, and no code in it can reach a provider
    until this task populates the payer columns. So this is the gate: a
    certified coder signs off on the code/qualifier pairings and the `assumes`
    annotations, and the AMA CPT licensing position is settled, before Track B
    fires a nudge at anyone. Do not close this without both.
  - **Test:** a SMART launch against local HAPI FHIR with a Synthea patient
    populates all three columns, and `resolve_query_parameters()` then returns a
    complete parameter set
  - **Test:** a `Coverage` resource with no plan type leaves the column NULL and
    the resolution names `plan_type` alone — never a default
  - **Test:** a CMS sub-state jurisdiction code reaching the state field is
    normalised to its parent state rather than stored raw
  - **Test:** end to end over Redis, replacing TASK-021's stubbed seam — a
    published transcript segment produces a real `/policies/query` call. This is
    TASK-024's deferred acceptance criterion and it belongs here, because it
    cannot pass until these columns are populated.
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
  - "At most one clinical_note per encounter" is an enforced invariant, not an
    assumption: TASK-030 added a UNIQUE constraint on
    `clinical_notes.encounter_id` and writes through `ON CONFLICT DO NOTHING`.
    Fetch the row expecting zero or one, and treat two as impossible rather
    than writing a tie-breaker for it.
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
  - **A visit outlasting the 15-minute JWT is settled in CLAUDE.md**, under
    "A visit outlasting the token re-mints" in the Session Lifecycle & JWT
    Issuance section — the same text TASK-025 cites on mobile. Do not re-derive
    it here, and in particular do not re-call `POST /sessions/start` to get a
    fresh token: that forks one visit into two encounters, silently.
    **TASK-006b has shipped** `POST /sessions/{session_id}/token` — refresh
    through it, proactively before opening a socket with a token near `exp` and
    reactively on `AUTH_REJECTED`. Only a 409 from it means the visit is over.
  - **Test:** component tests for start/active/end state transitions with mocked APIs

- [ ] **TASK-071:** Note review + edit UI
  - App: `apps/web`
  - `GET /notes/{session_id}` (TASK-032) — display generated SOAP note in an
    editable form, one text area per SOAP section. Keyed on the `session_id` the
    app already holds from `POST /sessions/start`; an earlier draft said
    `{encounter_id}`, an identifier no client is ever given.
  - Provider can edit any section; "Save" calls `PATCH /notes/{session_id}`
    (TASK-032), which sets `provider_edited = true` server-side. Send only the
    fields the provider changed — an omitted field is left alone, and the code
    lists specifically must not be sent as `[]` when they were untouched.
  - "Mark reviewed" sends `reviewed_by_provider: true` explicitly; loading the
    screen does not set it.
  - "Write to EHR" button calls fhir-integration's `POST /fhir/notes` (TASK-053)
  - Show ICD-10 and CPT codes extracted (from clinical_notes.icd10_codes /
    cpt_codes), editable as a tag-style list — allow add/remove, save via the
    same PATCH endpoint. Render `comprehend-medical` entries as machine
    suggestions, visibly distinct from the codes the provider is signing;
    accepting one re-sends it with `source: "provider-accepted"`, which is what
    makes it claimable by TASK-060. See CLAUDE.md's shape contract.
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

## Phase 8 — Provider Note Style Preferences

- [ ] **TASK-080:** Provider preferences schema + settings endpoints
  - Service: `services/track-a-clinical` (owns provider-scoped settings, same
    ownership logic as encounters)
  - New table `provider_preferences`:
```sql
    CREATE TABLE provider_preferences (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        provider_id UUID UNIQUE NOT NULL,
        style_preset VARCHAR(50) NOT NULL DEFAULT 'standard',
            -- 'standard' | 'concise-bullets' | 'full-narrative' | 'custom'
        custom_template TEXT,  -- physician-supplied example note or instructions,
                                -- required when style_preset = 'custom'
        section_order JSONB,   -- optional override, e.g. ["plan","assessment",...];
                                -- null means default SOAP order
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
```
  - New Alembic migration (own version_table per the established isolation pattern)
  - `GET /providers/{provider_id}/preferences` — returns current preferences or
    the default preset if none set
  - `PUT /providers/{provider_id}/preferences` — Pydantic request model validating
    `style_preset` against the enum, requiring `custom_template` when
    `style_preset == "custom"`
  - This route touches no PHI (it's provider configuration, not patient data) —
    no `audit_log()` call, standard INFO logging instead, per the corrected
    Known Constraints #6
  - **Test:** set a custom preference, retrieve it, verify round-trip
  - **Test:** submit `style_preset="custom"` with no `custom_template`, verify
    validation error

- [ ] **TASK-081:** Wire preferences into SOAP generation
  - Prerequisite: TASK-080, TASK-030 (existing SOAP generation)
  - Service: `services/track-a-clinical`
  - In TASK-030's SOAP generation call, fetch the provider's `provider_preferences`
    row before constructing the Sonnet prompt
  - Append a style-instruction block to the existing `SOAP_SYSTEM_PROMPT` based on
    `style_preset`:
    - `standard` — no change to existing prompt
    - `concise-bullets` — instruct terse bullet-point sections
    - `full-narrative` — instruct complete-sentence prose
    - `custom` — include `custom_template` verbatim as a style example for the
      model to match, with an explicit instruction that it's a style reference,
      not clinical content to copy
  - `section_order`, if set, reorders the returned SOAP sections in the response
    — this is post-processing on the model's output, not a prompt instruction
    (reordering via prompt is unreliable; do it in code after generation)
  - No change to the ICD-10/CPT Haiku extraction pass — style preferences apply
    only to the narrative SOAP text, never to code extraction accuracy
  - **Test:** generate a note for a provider with `concise-bullets` set, verify
    the Sonnet call's system prompt includes the style instruction
  - **Test:** generate a note with a `custom_template` set, verify it's included
    in the prompt and the response section order matches `section_order` if set
  - **Test:** provider with no preferences row — verify default `standard`
    behavior is unchanged from pre-TASK-080 behavior (regression test)

- [ ] **TASK-082:** Provider settings UI
  - App: `apps/web`
  - Settings screen: style preset selector (radio/dropdown), custom template
    text area (shown only when "custom" selected), section order (optional,
    lower priority — can ship without this control initially and default to
    server-side null)
  - Calls `GET`/`PUT /providers/{provider_id}/preferences` (TASK-080)
  - **Test:** component test for preset switching and custom template validation
    (mirrors TASK-080's server-side validation client-side for immediate feedback)

---

## Phase 9 — Prior Auth From Pasted or Uploaded Notes

- [ ] **TASK-090:** Note analysis endpoint (text input)
  - Prerequisite: TASK-030 (reuses its Haiku ICD-10/CPT extraction), TASK-012
    (policy query, unchanged)
  - Service: `services/track-a-clinical`
  - `POST /notes/analyze` — Pydantic request model:
    `{note_text: str, provider_id: UUID, patient_fhir_id: str | None,
    insurance_payer: str | None, insurance_plan_type: str | None, state: str | None}`
    — insurance/state fields are optional and nullable here, unlike the live-
    encounter path, since a pasted note has no FHIR Coverage lookup behind it;
    if omitted, the response includes a note that policy-query results are
    unavailable pending that information, rather than guessing
  - Reuses the exact Haiku extraction prompt from TASK-030 to pull ICD-10 codes,
    CPT codes, and identified procedures from `note_text` — do not write a second,
    slightly different extraction prompt; import/call the same function
  - If insurance/state fields are present, calls TASK-012's `/policies/query`
    per extracted procedure, exactly as TASK-021's live path does
  - This route touches PHI (`note_text` is clinical content) — **calls
    `audit_log()`** with `provider_id` as actor; there is no `session_id` for a
    manual submission, so hipaa-logger's audit_log signature needs a nullable
    `session_id` param if it doesn't already accept one — verify against
    TASK-002's actual signature before assuming
  - **Schema decision, made here rather than left open:** `prior_auth_requests`
    currently has `encounter_id UUID NOT NULL REFERENCES encounters(id)`. Add a
    migration making `encounter_id` nullable and add `source VARCHAR(20) NOT NULL
    DEFAULT 'encounter'` (`'encounter' | 'manual_note'`). This reuses the existing
    table, dashboard (TASK-072), and submission router (TASK-061) unchanged for
    manually-submitted bundles, rather than building a parallel table with its
    own dashboard support. Migration authored in track-a-clinical per the
    ownership rule.
  - **Test:** paste a sample orthopedic note with a clear MRI order, verify
    extracted codes and a resulting prior_auth_requests row with
    `source='manual_note'` and `encounter_id IS NULL`
  - **Test:** paste a note with no insurance info provided, verify response
    indicates policy check is unavailable rather than fabricating a result
  - **Test:** verify audit_log() call includes provider_id and a null session_id
    without erroring

- [ ] **TASK-091:** File upload support (PDF/text)
  - Prerequisite: TASK-090
  - Service: `services/track-a-clinical`
  - Extend `POST /notes/analyze` to accept a file upload (PDF or .txt) as an
    alternative to `note_text`, using the same `Annotated[..., File()]` pattern
    established in TASK-011
  - PDF text extraction via PyMuPDF — **this makes PyMuPDF a second/third
    consumer** (already used in `track-b-rag` for TASK-011, and possibly
    `policy-scraper`). Per the standing cross-service-boundary rule, extract
    PDF-text-extraction into a shared package (e.g. `packages/pdf-text`) rather
    than adding a second independent PyMuPDF integration. Check actual current
    usage across the repo before assuming which services need updating.
  - No OCR — if the PDF is a scanned image with no text layer, return an
    explicit error asking for a text-based file rather than silently returning
    empty extraction results
  - **Test:** upload a text-layer PDF, verify extraction matches a manually
    pasted version of the same content
  - **Test:** upload a scanned/image-only PDF, verify explicit error, not a
    silent empty-result response

- [ ] **TASK-092:** Note analysis UI
  - App: `apps/web`
  - Simple screen: paste-text area OR file upload, submit button, results view
    showing extracted codes and policy-check outcome (reusing TASK-042's
    nudge-style presentation for consistency, or a simpler static results card
    if the live nudge UI doesn't fit a non-live context well — use judgment,
    flag back if the existing NudgeOverlay component doesn't adapt cleanly)
  - **Test:** submit via paste, verify results render; submit via file, same

---

## Phase 10 — Scoped Assistant Chat

**Read this before starting any task in this phase.** This chat answers
questions about data the system has already computed — nudges fired, prior
auth status, payer policy rules already retrieved. **It never provides
clinical judgment, treatment suggestions, or diagnostic reasoning.** This
boundary is not a style preference; treatment-suggestion behavior is the
exact SaMD/liability risk this product has deliberately avoided everywhere
else. If a task in this phase is ambiguous about whether something crosses
that line, stop and flag it — do not resolve it by guessing toward "more
helpful."

- [ ] **TASK-100:** Assistant chat service scaffold
  - New service: `services/assistant-chat` (Python, FastAPI, port TBD — add to
    the Local Development port table)
  - Deliberately a separate service, not folded into track-a-clinical, so its
    tool access is enforced via HTTP calls to other services' existing
    endpoints, not direct DB access — this keeps its capability surface
    identical to what an external API caller could do, which is the easiest
    way to audit "can this chat see more than it should"
  - Bedrock model: **Sonnet** (`BEDROCK_MODEL_ID_REASONING`) — this involves
    conversational reasoning and tool selection, not mechanical extraction, so
    it follows the reasoning-task assignment, not the extraction one. Add this
    call site to CLAUDE.md's Bedrock Model Assignment table.
  - Standard `/health` endpoint per the established pattern (Bedrock reachability
    flag; no PHI, no audit_log call)
  - **Test:** health check reflects Bedrock connectivity

- [ ] **TASK-101:** Tool set — the enforcement mechanism, not a suggestion
  - Prerequisite: TASK-100
  - Define an explicit, closed set of callable tools — the model can ONLY
    retrieve data through these, never free-form query anything else:
    - `get_nudges_for_session(session_id)` → calls track-b-rag's existing
      nudge data (read-only, via an internal endpoint — add one to track-b-rag
      if it doesn't already expose nudge history by session, rather than
      reaching into its DB directly)
    - `get_prior_auth_status(session_id)` → calls prior-auth's existing data
    - `get_policy_rules(payer, plan_type, state, cpt_code)` → calls TASK-012's
      `/policies/query` Stage 1 (cacheable payer-policy fields only — never
      Stage 2 patient-specific fields through this path, since the chat isn't
      tied to live per-patient gap analysis)
  - No tool exists for: clinical history, medication lists, diagnostic
    reasoning, treatment suggestions, or anything not already listed above.
    Do not add a "general chart lookup" tool "for flexibility" — the narrow
    tool set is the safety mechanism, not an MVP limitation to expand later
    without the same scrutiny this phase's intro paragraph describes.
  - **Test:** verify the model cannot call anything outside this tool list
    (test the tool-calling harness's allowlist directly, not just prompt behavior)

- [ ] **TASK-102:** Conversation endpoint + refusal behavior
  - Prerequisite: TASK-101
  - `POST /chat/{session_id}/message` — Pydantic request `{message: str,
    provider_id: UUID}`, response `{reply: str, tool_calls_made: list[str]}`
    (surfacing which tools were used is useful for debugging and for an
    eventual audit trail of what data the chat actually touched per answer)
  - System prompt explicitly enumerates the tool set and states the refusal
    behavior: any question asking for a treatment recommendation, medication
    suggestion, diagnostic opinion, or "what should I do" framing gets a fixed
    refusal response, not a best-effort answer — e.g. "I can only answer
    questions about coverage requirements, nudges, and prior auth status for
    this visit. For clinical decisions, that's your call as the physician."
  - **This chat touches PHI** (session-scoped clinical/administrative data) —
    calls `audit_log()` per message, with `provider_id` and `session_id`
  - Store conversation history in a new `chat_messages` table (session_id,
    role, content, tool_calls, created_at) — migration authored in
    track-a-clinical per the ownership rule, even though the table is written
    by assistant-chat, consistent with the existing write-access-vs-migration-
    ownership split
  - **Test (the important one):** a fixed set of adversarial-shaped prompts
    ("what should I prescribe for X," "should I order an MRI," "what's your
    diagnosis of this patient") — verify every one gets the refusal response,
    not a best-effort clinical answer. This test suite should be treated with
    the same seriousness as TASK-012's fallback-safety tests; a single passing
    prompt that leaks a clinical suggestion is a shipped bug, not an edge case.
  - **Test:** a properly in-scope question ("why did the MRI nudge fire")
    correctly calls `get_nudges_for_session` and answers from its result
  - **Test:** verify `audit_log()` is called per message with correct actor/session

- [ ] **TASK-103:** Chat UI
  - App: `apps/web`
  - Simple chat panel, likely surfaced alongside the active session view
    (TASK-070) or the note review screen (TASK-071) — flag back which placement
    fits better once TASK-070/071's actual layout exists, don't guess at
    integration now
  - **Test:** send a message, verify reply renders; verify refusal responses
    render with the same visual treatment as normal replies (no special
    "blocked" styling that makes the refusal feel like an error rather than a
    designed boundary)

---

## Known Constraints for Claude Code

1. **Do not install Kafka.** Redis pub/sub is the message bus. Any task that says "publish event" means `redis.publish()`.

2. **AWS Bedrock, not direct Anthropic API.** All LLM calls use `langchain_aws.ChatBedrock` with `boto3.client('bedrock-runtime')`. Never import `anthropic` directly.

3. **Moto for all AWS mocking in tests.** `@mock_aws` decorator on test functions
   that call Bedrock, Transcribe Medical, KMS.
   **Comprehend Medical is a standing exception** — moto does not implement it
   at all, confirmed by a live `@mock_aws` call returning
   `404 Not yet implemented` on `InferICD10CM`. See CLAUDE.md, "Moto does not
   implement Comprehend Medical", for the two permitted alternatives (a gated
   real call, or explicitly-labelled synthetic fixtures) and for why a silent
   `unittest.mock` patch does not count as satisfying this rule. If another AWS
   service turns out to be uncovered, it goes in that section too rather than
   being worked around locally.

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
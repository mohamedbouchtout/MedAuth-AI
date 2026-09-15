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
│   ├── api-envelope/     # Shared HTTP response envelope + FastAPI error handlers
│   ├── hipaa-logger/     # Shared audit logging — every service imports this
│   ├── logging-policy/   # What third-party libraries may log — every service installs this
│   ├── fhir-types/       # Shared FHIR R4 type definitions (Python + TypeScript)
│   ├── audio-wire/       # Encounter-audio wire format — both frontends (TypeScript)
│   ├── session-client/   # Session lifecycle client + token freshness — both frontends (TypeScript)
│   ├── nudge-client/     # Nudge payload contract + acknowledge call — both frontends (TypeScript)
│   ├── fhir-client/      # SMART launch + patient identity client — both frontends (TypeScript)
│   ├── session-auth/     # Session-token validation for every real-time endpoint
│   ├── cors-policy/      # The one CORS policy, installed by browser-facing services
│   ├── payer-vocab/      # Canonical payer slugs + USPS jurisdiction codes
│   ├── html-digest/      # Which HTML bytes are script/style; the policy digest without them
│   ├── bedrock-client/   # Shared Bedrock access — client construction + reading replies
│   └── crypto-utils/     # AES-256 helpers used across services
├── infrastructure/
│   ├── terraform/        # AWS infrastructure as code
│   └── kubernetes/       # K8s manifests + Helm chart
├── scripts/
│   ├── seed-synthea.sh   # Load synthetic patients into local HAPI FHIR (TASK-052)
│   └── setup-dev.sh      # One-command dev environment setup (TASK-052c)
└── docker-compose.yml    # Full local stack — postgres, redis, qdrant, hapi-fhir, crd
```

## Tech Stack

### Python Services (all of `services/`, including fhir-integration)
- **Runtime:** Python 3.12, one uv workspace for every service and package
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
- **FHIR client (fhir-integration):** fhirclient (pip package)
- **Testing:** pytest + pytest-asyncio, httpx for async client testing
- fhir-integration is Python, not Node.js — earlier drafts were wrong.

### Frontend (apps/web)
- **Framework:** React 19 + TypeScript (matches `apps/mobile`'s React via Expo SDK 57;
  one React major per npm workspace root).
- **Build:** Vite
- **Styling:** Tailwind CSS
- **State:** Zustand — not installed until a task has state to keep in it.
- **FHIR:** fhirclient.js for SMART on FHIR OAuth flow
- **WebSocket:** native WebSocket API (no socket.io)
- **Audio:** `getUserMedia` → `AudioContext({ sampleRate: 16000 })` →
  `AudioWorkletNode`, with the float-to-int16 conversion and 250ms framing in
  `packages/audio-wire`. **Not MediaRecorder** — it cannot emit raw PCM, offers no
  sample-rate control, and its `timeslice` chunks are not independently decodable
  (measured, TASK-023).
- **Testing:** Vitest + React Testing Library

### Frontend (apps/mobile)
- **Framework:** React Native with Expo SDK 57
- **Audio:** `expo-audio`'s `useAudioStream` — real-time PCM buffers to an `onBuffer`
  callback, never a recorded file. **Not `expo-av`**: it records to a file URI, has no
  PCM callback, and was removed in SDK 55. `useAudioStream` needs SDK 56+. See TASK-022.
- **Testing:** Jest + React Native Testing Library

### Infrastructure
- **Cloud:** AWS (us-east-1) — all HIPAA-eligible services, BAA signed
- **Containers:** Docker + EKS (Kubernetes)
- **IaC:** Terraform
- **CI/CD:** GitHub Actions

## Local Development

### Prerequisites
```bash
docker & docker compose
uv (pip install uv)
node 24+ & npm  # version lives in .nvmrc; CI reads that file
aws cli (configured with dev credentials)
```

### Start full local stack
```bash
docker compose up          # Postgres, Redis, Qdrant, HAPI FHIR, CRD RI
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
8006  Da Vinci CRD Reference Implementation (simulated payer, TASK-015)
      Container listens on 8090; published as 8006 because Windows reserves 8081-8180.
8001  audio-ingestion
8002  track-b-rag
8003  track-a-clinical
8004  fhir-integration
8005  nudge-service
8007  prior-auth (health endpoint, TASK-060; 8006 was taken by CRD)
5432  PostgreSQL
6379  Redis
6333  Qdrant
```

### Environment variables for local dev
Copy `.env.example` to `.env.local`. Never commit `.env.local`.
For local dev, AWS credentials use the `medauth-dev` IAM profile.
Bedrock is the only AWS service called during local dev (no local mock available).
A variable in `.env.example` is not wired until a config module reads it — frontends
read env only in `apps/*/src/config.ts`, services only through their `Settings` class.

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
- Moto for mocking AWS services (Bedrock, Transcribe Medical, KMS) — but see
  "Moto does not implement Comprehend Medical" below.
- Test files mirror src structure: `src/services/rag.py` → `tests/unit/services/test_rag.py`
- Minimum 80% coverage on services/packages; CI fails below this
- **A browser-facing route gets its own preflight case in that service's
  `test_cors.py`, in the change that makes it browser-facing**, with an
  unlisted-origin counterpart. Installed middleware is never evidence that a
  particular path and method are covered: `packages/cors-policy` fixes methods and
  headers repo-wide, so a route can still be refused. A route already called from
  `apps/mobile` still needs a case the first time a browser calls it; mobile
  preflights nothing. (Three tasks rediscovered this gap; see "CORS and browser
  reachability".)

### Moto does not implement Comprehend Medical (standing exception)
Permanent property of moto (verified on 5.2.2): `infer_icd10_cm` under `@mock_aws`
returns `404 Not yet implemented`. Moto's `comprehend` module is Amazon Comprehend, a
different service. The 404 is not a bug in the call.

**Permitted alternatives** (for this and any other AWS service moto lacks):
- **A real credentialed call behind an env-var gate**, following the
  nightly-live-checks.yml rules below — default off, paired with a scheduled run,
  named after the dependency.
- **Hand-rolled fixtures explicitly labelled synthetic**, stating in the fixture
  module that they are approximations and what has never been checked against the
  real service.

**Not permitted: a silent `unittest.mock` patch over the boto3 call presented as
satisfying the moto rule.** Such a test asserts only that code calls a function the
test defined. If moto cannot cover a service, the test must say so visibly.

### Raw sync boto3 calls in async contexts go through `asyncio.to_thread`
boto3 is synchronous; calling it inside an asyncio task blocks the event loop for the
whole AWS round trip and stalls every other live encounter on the pod. (The Bedrock
path via `ChatBedrock.ainvoke` is genuinely async and unaffected.) **Every raw
synchronous boto3 call made from an async context is wrapped in
`asyncio.to_thread`** — a general rule for every new direct AWS SDK call site.

### Git Commits
- **50/72 rule.** Subject ≤ 50 characters; body hard-wrapped at 72 columns. The
  subject still carries type, scope, and task number, so keep it terse.
- Subject is imperative mood, no trailing period.
- Blank line between subject and body. Trailers (`Co-Authored-By:`) go last.
- **Every commit must pass CI on its own**, not just the branch tip. Land tooling
  and config before the code that depends on them; squash or reorder any "fixes
  the previous commit" commit before opening a PR — history stays bisectable.
- One logical change per commit. CI changes, docs, and feature code are three
  commits, not one.
- Every commit message should have my sign-off at the end, co-authored by you
  when you contributed to the commit.

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
- **No Kafka until >20 providers.** Redis pub/sub for now; interfaces are identical so swapping is a config change.
- **Qdrant for vector store.** Not Pinecone or Weaviate — self-hosted for PHI control (defense in depth).
- **Cache the payer-policy half of RAG results in Redis** with 24h TTL keyed by
  `rag:{payer}:{plan_type}:{state}:{cpt_code}` — a major cost lever. **Cache only
  the payer-policy fields** (`requires_auth`, `auth_criteria`,
  `step_therapy_required`, `step_therapy_details`), which are identical for every
  patient on that payer/plan/state/CPT. **Never cache** `missing_criteria`,
  `denial_risk`, `nudge_message` — they describe *this patient's documentation*;
  caching them would serve patient B the gaps computed for patient A. Recompute
  them on every call against the cached rules. Do not add `clinical_context` to
  the key (correct, but collapses the hit rate to zero).
- **Haiku for extraction tasks, Sonnet for reasoning.** ICD/CPT extraction → Haiku. SOAP generation and payer policy analysis → Sonnet.
- **Policy lookup is two-tier, and the tiers answer different questions.** For
  CMS-0057-F mandate payers (Medicare Advantage, Medicaid managed care, CHIP, ACA
  marketplace), `/policies/query` (TASK-012) asks the payer's Da Vinci **CRD**
  endpoint (TASK-015) *and* runs the RAG path (TASK-010–014) concurrently. CRD
  decides `requires_auth`; RAG supplies `auth_criteria` and step therapy fields.
  Commercial employer plans take the RAG path alone. Same response shape either
  way; callers never branch on which answered.
  - **CRD does not carry the criteria** — a property of the standard: its
    `ext-coverage-information` extension has `covered`, `pa-needed`, `doc-needed`,
    etc., but no criterion text (that lives in a DTR Questionnaire). So RAG is never
    skipped. Da Vinci **DTR** is deferred: it needs a Phase 5 SMART app surface, and
    its items are mostly administrative fields that would poison the Stage 2 matcher.
  - **Silence from a payer is never "no authorization required."** An empty card
    list, a documentation-only card, and an "unable to process" card all mean *no
    determination*; RAG then answers alone. Reading any as negative would clear an
    unauthorized order — the one direction TASK-012 forbids failing in.
  - **The CRD request carries no patient.** It is built from payer, plan type,
    state and procedure code with a placeholder subject; age/sex-keyed rules return
    "unable to process" and RAG answers. Never fabricate a patient. Closing this is
    TASK-059 (gated on TASK-052); once a patient is sent it becomes a PHI disclosure
    needing TLS, per-payer endpoint verification, and its own audit row.
- **A CRD answer is never cached; a RAG answer is.** CRD's value is being live and
  authoritative at order time. The CRD path neither reads nor writes the `rag:` key.
  If CRD latency threatens the nudge budget, use a short seconds-scale TTL of its
  own, never the 24h key.

### Session Lifecycle & JWT Issuance (read before Phase 1/2/3/4 tasks)
Implemented as **TASK-006** (prerequisite for TASK-020, 021, 030, 041, 060).
- `services/track-a-clinical` owns session lifecycle, because it owns `encounters`.
- `POST /sessions/start` — body `{patient_id, provider_id, ehr_encounter_id}`.
  Creates an `encounters` row (`status='active'`; wire `patient_id` → column
  `patient_fhir_id`), mints a JWT with claims `{session_id, provider_id, exp}` — no
  `iss`/`aud` in v1 (the unused `JWT_ISSUER`/`JWT_AUDIENCE` vars wait on a later
  hardening task that must also grow the validators). Lifetime from
  `SESSION_TTL_SECONDS` (default 900). `session_id` is a server-generated UUID.
  Returns `{session_id, jwt}`. **Publishes `session_id` to `sessions:started`
  before responding**, so a consumer is subscribed before the client can open its
  audio socket. A failed publish is a 503 — an unwatched encounter raises no nudges
  and looks like one with nothing to flag.
- `POST /sessions/{session_id}/end` — sets `status='completed'`, `ended_at=NOW()`,
  publishes `session:ended:{session_id}` (empty payload; the trigger for TASK-030
  SOAP generation and TASK-060 bundle assembly). Unknown/soft-deleted → 404.
  Repeat-ending a completed session → 200, idempotent, **no second publish** (it
  would duplicate SOAP generation and bundle assembly).
- `audio-ingestion` (TASK-020) and `nudge-service` (TASK-041) validate the JWT
  before accepting a WebSocket. Signing secret `JWT_SIGNING_KEY`, HS256.

**A visit outlasting the token re-mints; the encounter never ends because a
token expired.** Visits routinely exceed 15 minutes. Both apps (TASK-025 mobile,
TASK-070 web) follow these rules and do not re-derive them.

- **The token bounds connection establishment, not stream lifetime.** Endpoints
  validate once before the handshake (`_authenticate` before `accept()` in
  `services/audio-ingestion/src/api/websocket.py`) and never re-validate an open
  connection. Expiry only matters when a *new* socket opens (reconnect, or the
  nudge socket opening later).
- **Re-mint for the same `session_id`. Never by calling `POST /sessions/start`
  again.** A second start forks one visit into two encounters: split transcript
  channels, two partial SOAP notes, a half bundle, and `procedure_seen:` no longer
  dedups — all silently.
- **The endpoint that re-mints without starting a session is
  `POST /sessions/{session_id}/token`** (TASK-006b). Returns `{session_id, jwt}`
  with **200, not 201**; writes nothing beyond its audit row; publishes nothing.
  Unknown/soft-deleted → 404; `completed` encounter → **409** (a finished visit
  must not reopen an audio socket).
- **Refresh proactively and reactively**: before opening any new socket when the
  held token is near `exp`, and on `AUTH_REJECTED` from a socket that failed to open.
- **The credential is the session's own token, in an `Authorization: Bearer`
  header, expired or not** (header carrier only). Validation matches
  audio-ingestion's — signature, required claims, `session_id` claim equals path —
  except expiry is tolerated within `SESSION_REMINT_GRACE_SECONDS` past `exp`
  (**default 3600 is an unvalidated assumption; treat as provisional**). A re-mint
  endpoint is exactly as strong as the sockets its tokens open; no provider auth
  exists before Phase 5. The grace window bounds how long a *captured* token stays
  useful, since nothing auto-completes an abandoned encounter.
  **Re-minting revokes nothing** (issue #51): no `jti`, no token store, so every
  token in the window stays valid. Ending the encounter is the only revocation.
- **The provider comes from the `encounters` row, never from the presented
  token's claim.**
- **A refreshed token does not extend the encounter.** Token lifetime and visit
  lifetime are independent; only `/end` ends a visit.

**Session-scoped routes are keyed on `session_id`, and the note routes carry no
credential in v1.** (Answers Known Constraints #8.)
- **`session_id` is the only identifier this service exposes to clients.** Nothing
  hands out `encounters.id`; `GET`/`PATCH /notes/{session_id}` (TASK-032) key on the
  session. Exposing both would give clients two names for one visit.
- **Those routes take no session token in v1, and `actor_id` comes from the
  `encounters` row** — matching the strength of everything around them (unauthed
  `provider_id` on start, possession-only tokens, no provider auth before Phase 5).
- **`validate_remint_credential` is deliberately *not* reused here.** It 409s on
  completed encounters, and notes exist *only* on completed encounters. Do not
  "fix" the note routes by adding it; Phase 5's credential must treat completed as
  the normal case.
- **Note access is still PHI and is still audited** (`READ_NOTE`, `UPDATE_NOTE`).

**A route keyed on a resource rather than a session follows the same v1 rule.**
`PATCH /nudges/{nudge_id}/acknowledge` (TASK-041b) is keyed on the
`clinical_nudges` PK (the only id the nudge payload gives a client).
- **No credential, and audit instead** — the reasoning never depended on path shape.
- **`packages/session-auth` does not fit**: it compares the token's `session_id` to
  one in the path, which this path lacks. Do not add a path segment just to fit it.
- **`actor_id` is resolved through the resource** — `nudge_id` →
  `clinical_nudges.encounter_id` → `encounters.provider_id` — never from the caller.
- **The eventual fix is a *resolved* `session_id`**: generalise session-auth to
  compare the token's claim to the session owning the resource. Do it when a second
  or third non-session-keyed browser route appears or Phase 5 auth lands. Not built
  for one route — a deliberate v1 trade.
- **This settles credentials and says nothing about CORS** — see "CORS and browser
  reachability".

**How the JWT reaches a WebSocket endpoint — either carrier, never both
required.**

```
Authorization: Bearer <jwt>                                     # header carrier
Sec-WebSocket-Protocol: medauth.session.v1, medauth.jwt.<jwt>   # subprotocol carrier
```

The header is for service callers and tests. Browsers cannot set headers on the
native `WebSocket`, so the subprotocol carrier exists for them (TASK-023 must use
it). Every real-time endpoint supports both, implemented once in
`packages/session-auth`.
- **Validation is identical whichever carrier was used** — signature against
  `JWT_SIGNING_KEY`, `exp` in the future, `session_id` claim equals URL path.
- **Reject before the handshake completes.** Close code 4401. (Below ASGI a
  pre-handshake refusal is an HTTP status on the upgrade, so browsers see a failed
  upgrade rather than `onclose` 4401 — the correct trade.)
- **The server echoes `medauth.session.v1` and never the token** — echoing the
  `medauth.jwt.` entry would write the credential into response headers and proxy
  logs. Clients offer the version marker first so there is something safe to echo.
- **A token carried this way is still a credential**: never logged, never in an
  error message, never in a URL query string.

### A SMART launch is not an encounter session (cross-cutting)
**Three identifiers, never equal, none derivable from another, three lifetimes:**
- **`session_id` — the encounter session.** Minted only by `POST /sessions/start`
  (track-a-clinical). Keys every Redis channel, real-time endpoint and
  session-keyed route. Lives for the visit; only `/end` ends it.
- **`launch_id` — the SMART on FHIR OAuth launch.** Minted by `GET /fhir/launch`
  (fhir-integration, TASK-051). Names one authorization flow and its EHR token;
  lives as long as the EHR says.
- **`ehr_encounter_id` — the encounter as the EHR knows it.** Minted by the EHR,
  passed to `/sessions/start`, stored on `encounters.ehr_encounter_id`. Outlives both.

`session_id` and `ehr_encounter_id` name the same visit in two namespaces;
`launch_id` names something else. Consequences:
- **`ehr_encounter_id` is unique only within one EHR** — never treat it as global
  or key anything of ours on it alone.
- **Every `/fhir/*` route keyed on an encounter takes `ehr_encounter_id`**, never
  `session_id` (`GET /fhir/encounter/{encounter_id}`, `/coverage-context`, note
  write-back).
- **It is nullable** — a session started outside a SMART launch has none, and
  anything needing it (note write-back) must handle absence explicitly.
- A launch precedes the encounter (no `session_id` exists at `/fhir/callback`), one
  launch can outlive several encounters, and an EHR token can renew mid-visit.
- **Redis keys are `fhir_launch:{state}` and `fhir_token:{launch_id}`** — never
  `fhir_session:*` or anything keyed on `session_id`. The word `session` stays out
  of the OAuth flow's vocabulary.
- TASK-052b records an explicit encounter → `launch_id` mapping (never assumes
  equality). TASK-070 holds both and sends whichever the route is keyed on. TASK-053
  holds all three — three parameters, no defaulting one from another.
- **No route accepts them interchangeably** — the wrong one is a 404.
- **They never share a key namespace.** `ehr_encounter_id` keys nothing of ours.

### Handing a completed SMART launch back to a client (cross-cutting)
Settled by TASK-051f; TASK-025c (mobile) and TASK-070 (web) cite this.

**The gap:** `GET /fhir/callback` answers `{launch_id, ehr_type, expires_in}` as
JSON in the browser, which no app can read. **Never redirect with
`?launch_id=...`** — a `launch_id` resolves to an EHR token (a capability handle)
and must not be in a URL; a fragment is no better.

#### The callback keeps its JSON answer and gains a second delivery
- **Which answer a launch gets is declared by whoever initiated it — never
  inferred, and never chosen as a default.** `GET /fhir/launch` takes an explicit
  `delivery` param, stored on the `fhir_launch:{state}` record and read back by the
  callback from the claimed record.
- **Closed vocabulary `LaunchDelivery` (`StrEnum`)** — it round-trips through Redis.
- **`delivery=json`**: existing answer, unchanged.
- **`delivery=web` / `delivery=mobile`**: redirect to that platform's configured
  return target carrying a claim code, not the `launch_id`.
- **Absent `delivery`** = no client is waiting (EHR-initiated or service caller) →
  JSON. This is the unchanged original behaviour, not a chosen default.
- **Nothing sniffs the request** (`Accept`, `User-Agent`, etc.).

#### The handoff is a single-use claim code, exchanged over POST
- **`fhir_launch_claim:{claim}`** holds `launch_id`, `ehr_type`, access-token
  expiry under a short TTL.
- **`POST /fhir/launch/claim`** exchanges it for `{launch_id, ehr_type,
  expires_in}` — POST, never GET.
- **Redemption is atomic and single-use** (`GETDEL`, as `claim_launch()`). Unknown,
  expired and already-redeemed are one 404 answer.
- **The claim code is not the capability handle** — like OAuth's code vs. token.
- No access token, refresh token or scope reaches the client. Neither `launch_id`
  nor claim code is ever logged.
- **Known limit:** a claim code intercepted in the redirect (hostile app
  registering the same URI scheme on Android) could be redeemed within the TTL.
  Narrow because the window is seconds and a race shows as a failed launch. Upgrade
  path: bind the claim to a client verifier reusing `src/smart/pkce.py`. Not built;
  revisit only when interception is demonstrated on a real platform.

#### Two return targets, two settings, both bound and both validated
- **`SMART_WEB_RETURN_URL`** — absolute `https://`; `http://` only for `localhost`
  / `127.0.0.1`.
- **`SMART_MOBILE_RETURN_URI`** — custom scheme (e.g. `medauth://launch`);
  `http`/`https` refused.
- **Neither may carry a query string or fragment** (the claim code is appended).
- **Both are validated at startup and a bad or missing one refuses to boot** —
  otherwise the error surfaces at the end of an OAuth chain after a real credential
  is spent. Single-platform deployments still set both.

#### This route touches no PHI, so it logs and does not audit
`POST /fhir/launch/claim` returns a `launch_id`, vendor name and seconds — logs at
INFO, no `audit_log()` (Known Constraints #6 is if-and-only-if). PHI reads under the
launch audit as usual.

#### A failed launch is delivered the same way, and carries no claim code
Settled by TASK-051g. Without it, a declined `delivery=web` launch strands the
provider on a JSON error page on fhir-integration's origin, and the web app cannot
tell "declined" from "still signing in" from "failed".
- **A failure on a `web`/`mobile` launch redirects to that platform's return
  target** carrying a fixed error code instead of a claim code.
- **`delivery=json` and absent `delivery` keep raising unchanged.**
- **No claim code and no `fhir_launch_claim:` record on failure.**
- **Closed vocabulary `LaunchFailure` (`StrEnum`), not a diagnostic channel.** Two
  members: `declined` (the authorization server refused) and `failed` (anything
  else). Both mean "launch again"; no third member.
- **The EHR's own refusal reason is dropped at this boundary** — logged, never
  forwarded to our UI.
- Unlike the claim route, `declined` vs `failed` are *not* collapsed: the person at
  the browser already knows whether they clicked "deny".

**One failure structurally cannot redirect, and no code may be added that makes
it.** When `claim_launch()` returns `None` (unknown/expired/replayed `state`) there
is no record, so `delivery` is unknowable and the callback raises for every launch.
Never read `delivery` off the callback's own request — that would let anyone choose
where this service redirects a browser. Only failures after the record is in hand
(AS refused, no code, token exchange failed) are delivered.

**Both apps render an error delivery as a failed launch**, distinct from the plain
sign-in screen (web) and read *before* the "return URI misconfigured" message
(mobile). **The vocabulary is shared through `packages/fhir-client`; the URL
parsing and the wording are not** (browser `location.search` vs. React Native's
partial `URL` differ; the package holds no UI).

### Which EHR a client-initiated standalone launch targets (cross-cutting)
Settled by TASK-025c; TASK-070 follows it. Only standalone launches need this (an
EHR-initiated launch carries `iss`).
- **`iss` is read from configuration — `EXPO_PUBLIC_SMART_ISS` on mobile,
  `VITE_SMART_ISS` on web — never a literal in source.** It is a public FHIR base
  URL, not a credential. If unset, the app offers no standalone launch and says so —
  never a launch against an empty issuer.
- **One configured `iss` per deployment is a deliberate scope limit tied to the
  current pilot stage. It is not an architectural choice, and must not be read as
  one.** Issuers are per-practice and per-vendor; today there is one pilot target
  (Athenahealth).
- **The trigger to revisit is the second EHR-or-practice combination being
  onboarded**, at which point the issuer becomes a provider-facing selection at
  launch time rather than build-time configuration.

### Writing clinical data out to the EHR (cross-cutting)
Applies to TASK-053 (note → `DocumentReference`), TASK-054 (prior-auth submission)
and every later outbound writer.

- **Nothing a machine merely suggested may leave this system.** Every outbound
  writer filters `icd10_codes`/`cpt_codes` on `source`, sending only
  `llm-extraction` and `provider-accepted`. A `comprehend-medical` entry becomes
  sendable only when a provider accepts it via `PATCH /notes/{session_id}`. Apply
  the filter by default and cite this section at the filter site.
- **An outbound write is its own audited event, under its own action** — distinct
  from `WRITE_NOTE` (stored here), added to `AuditAction` in the change that writes it.
- **One outbound write produces an audit row in each service that acted** —
  fhir-integration (sent to EHR; actor `fhir_practitioner_ref`) and
  track-a-clinical (row mutated; actor `encounters.provider_id`). Not double counting.
- **A write-back never sets a provider-attestation flag** (`reviewed_by_provider`).
- **The service that owns the table records the result, over HTTP.**
  fhir-integration (no DB connection, deliberately) calls a server-to-server route
  on track-a-clinical to store the document id. `PATCH /notes` keeps forbidding
  server-owned fields.
- **PHI takes the shortest path, which means the server fetches it.** fhir-integration
  reads the note from track-a-clinical over HTTP (which audits `READ_NOTE`); the
  client never posts the note body back.
- **A cross-service call needs a bound setting, not an `.env.example` line** — the
  target base URL is read by the caller's config class.
- **Order an outbound write so the recoverable failure is the one that happens.**
  External write first, local record second. If the second step fails, name the
  created document id in the error and log at ERROR — do not report total failure
  (it invites a duplicating retry).
- **Duplicate clinical documentation is a real harm, so a repeat write is
  refused** before any EHR call. A genuine replacement would be a separately named
  operation using FHIR `status`/`relatesTo`.

### CORS and browser reachability — decided once, in the services (cross-cutting)
Settled by TASK-041c; TASK-042/043 and all browser work cite it.

**The policy is `CORSMiddleware` installed in each service from one shared
package (`packages/cors-policy`).** Not an ingress, gateway, or per-service
allow-lists. Origins, methods, headers and credentials come from per-environment
config (`CORS_ALLOWED_ORIGINS`) — never hardcoded, never `*` on a PHI service.
- **One shared package, imported** — same pattern as api-envelope and session-auth.
- **A new package, not part of `api-envelope`**, whose locked scope excludes
  middleware. It has its own path filter, CI job and 80% gate.
- **Only the services that answer HTTP to a browser install it**: track-b-rag,
  track-a-clinical, fhir-integration and prior-auth (check call sites rather than
  trusting this list). `audio-ingestion` and `nudge-service` serve WebSockets +
  `/health`; browsers apply no CORS to upgrades. Add it when a service grows a
  browser-facing HTTP route, not pre-emptively.
- **Installing it is not covering a route.** Each installing service keeps a
  `test_cors.py` asserting its browser callers' path-and-method combinations, with
  an unlisted-origin counterpart. New browser-facing routes get a case in the same
  change — including routes already used by mobile.
- **Why not an ingress:** local dev and CI run `docker compose` with no proxy and
  `apps/web` talks straight to service ports, so ingress-only CORS would leave dev
  and CI without an answer.
- **The gateway question is deferred to Phase 6, not answered "unnecessary"** — an
  ingress is the likely home for TLS termination, routing and rate limiting.
- **When a gateway does arrive, the middleware comes out in the same change** —
  duplicated `Access-Control-Allow-Origin` headers make browsers reject every route.

**Choosing per-service CORS forecloses nothing about where authentication
lands.** The auth check this repo needs (resolved `session_id` via
`nudge_id` → `clinical_nudges.encounter_id` → `encounters.provider_id`) is a
domain-table join that belongs in the service regardless; a gateway could only
split auth, never unify it. This section decides only CORS.

**WebSocket handshakes are outside CORS, and the `Origin` check added here is
defence in depth rather than a fix.**
- **Why the absence of a check was not a hole:** cross-site WebSocket hijacking
  rides ambient credentials (cookies); ours travel in a header or subprotocol a
  hostile page would already need to hold.
- **Why the check is added anyway:** nearly free given `CORS_ALLOWED_ORIGINS`, and
  nudge-service's sockets (nudge stream and transcript stream, both through one
  `serve_stream`) carry live PHI.
- **Do not conclude** it fixed a vulnerability or that removing it reopens one.
  **If the credential ever moves to a cookie, the check stops being optional.**

### Redis Key Naming — Canonical List
Use these exact patterns; do not invent variants:
```
transcription:{session_id}    pub/sub — transcript segments. Published by
                              audio-ingestion (TASK-020); consumed by
                              track-a-clinical (TASK-030), track-b-rag (TASK-021),
                              nudge-service (TASK-041d, relays verbatim). Shape:
                              "The transcript segment payload — one shape". `text`
                              is PHI and never reaches a log line.
nudges:{session_id}           pub/sub — nudge events. Published by track-b-rag
                              (TASK-040); consumed by nudge-service (TASK-041).
                              Shape: "The nudge payload — one shape".
session:ended:{session_id}    pub/sub — empty-payload signal from track-a-clinical
                              (TASK-006); consumed by track-a-clinical (TASK-030),
                              prior-auth (TASK-060), track-b-rag (TASK-021).
sessions:started              pub/sub — fixed channel, payload {"session_id": ...}.
                              Published by track-a-clinical before /sessions/start
                              returns; consumed by track-b-rag (TASK-021) so it can
                              subscribe by name instead of wildcarding
                              transcription:*. A token re-mint (TASK-006b)
                              publishes nothing.
procedure_seen:{session_id}   set, 4h TTL — procedure keys already queried this
                              encounter, so one procedure raises one nudge
                              (TASK-021). Members `cpt:{code}` or
                              `keyword:{keyword}` when no CPT resolves (TASK-024).
                              Claimed via SADD; deleted on session:ended.
rag:{payer}:{plan_type}:{state}:{cpt_code}
                              cache, 24h TTL — payer-policy fields ONLY
                              (requires_auth, auth_criteria, step_therapy_required,
                              step_therapy_details). Never missing_criteria,
                              denial_risk, nudge_message. (TASK-012)
fhir_launch:{state}           cache, ~10 min — transient SMART launch state (iss,
                              ehr_type, token endpoint, PKCE verifier, delivery).
                              Single-use; the callback deletes it. (TASK-051)
fhir_token:{launch_id}        cache, TTL = refresh grant lifetime, NOT the access
                              token's — access + refresh token, token_endpoint,
                              fhir_base_url, ehr_type, access_token_expires_at.
                              Keyed on launch_id, never session_id. (TASK-051/051b)
fhir_launch_claim:{claim}     cache, ~2 min — single-use handoff code (launch_id,
                              ehr_type, token expiry) written only for
                              delivery=web|mobile; consumed atomically by
                              POST /fhir/launch/claim. (TASK-051f)
```
Lowercase, colon-separated, most-specific segment last. A new pattern is added to
this list in the same PR. `{payer}` is always the canonical slug from
`packages/payer-vocab`, never a display name.

### The launch record outlives its access token (reverses a TASK-051 rule)
**Deliberate reversal, not a regression.** TASK-051 set `fhir_token:{launch_id}`'s
TTL to the EHR's `expires_in`, which deleted the only copy of the refresh token at
the moment renewal was needed — making TASK-051b impossible.

**What holds now:**
- **The key's TTL bounds the refresh grant, not the access token.**
- **The access token's own expiry is a field** (`access_token_expires_at`,
  absolute UTC), never inferred from the key's TTL.
- **A launch with no refresh token keeps the old behaviour** — the record expires
  with the access token.
- **The TTL is bounded and configurable**: `SMART_LAUNCH_RECORD_TTL_SECONDS`,
  default 8h (a round stand-in for a clinic day, not a measurement). It bounds how
  long a compromised Redis yields a usable credential — do not raise casually.
- **The record is still a credential store**: the refresh token never appears in a
  log line, exception message or `repr`.

**Renewal is proactive, at the point the adapter is built**: `get_ehr_adapter` in
`services/fhir-integration/src/api/fhir.py` refreshes when expiry is within
`SMART_TOKEN_REFRESH_SKEW_SECONDS`, so no route or adapter primitive knows renewal
exists. (Reactive 401-retry would need a wrapper at every fetch site.) Revoked
grants and clock skew beyond the margin still yield 401 `FHIR_LAUNCH_EXPIRED` and
a relaunch; revisit only for a real vendor whose skew exceeds the margin.

**A refresh that fails is two different outcomes, and only one ends the launch.**
- **OAuth-level rejection (4xx, e.g. `invalid_grant`)** → grant gone → 401
  `FHIR_LAUNCH_EXPIRED`; record rewritten with the refresh token dropped and a
  short TTL.
- **Transport failure or 5xx** → unknown → record untouched → 502
  `FHIR_TOKEN_REFRESH_UNAVAILABLE` (504 on timeout), retryable.

**Refreshing an EHR token neither extends nor ends an encounter**, and `launch_id`
never changes. **Renewal writes no audit row** — obtaining a credential is not
using it.

### The transcript segment payload — one shape (cross-cutting)
What rides on `transcription:{session_id}`. Writer: `encode_segment` in
audio-ingestion `src/publisher.py`. Readers: track-a-clinical (TASK-030),
track-b-rag (TASK-021), nudge-service relay (verbatim, TASK-041d), TASK-070 browser.

```json
{
  "session_id": "0b7f1e2c-...",
  "result_id": "a1b2c3d4-...",
  "text": "Patient reports right knee pain for about six weeks.",
  "is_partial": false,
  "start_time": 12.34,
  "end_time": 16.78
}
```

| Field | Type | Notes |
|---|---|---|
| `session_id` | `str` (UUID) | Repeated inside the payload so a multiplexing consumer need not parse the channel name. |
| `result_id` | `str` | Transcribe Medical's utterance id. Key idempotency on this, not on text equality. |
| `text` | `str` | What was said. **Never empty** — empty/no-alternative results are dropped. **PHI.** |
| `is_partial` | `bool` | **Always `false` on the bus today, and the field still travels.** |
| `start_time` | `float \| None` | Seconds from stream start, passed through from Transcribe. |
| `end_time` | `float \| None` | Same, for utterance end. |

- **`text` is PHI** — the largest body of it in the repo. No log line anywhere on
  this path; log session ids, character counts or close reasons only.
- **Only stabilized results are published.** `publish_segment` drops partials
  (they would multiply traffic and fire duplicate procedure nudges). Readers must
  still skip `is_partial: true` — it lets a later task forward partials without a
  shape change.
- **A reader narrows, it does not trust**: validate the fields above, ignore others.
  If both frontends read this shape, it moves into a shared TS package.
- **Nothing reshapes it in transit** — the relay forwards the exact string and
  never models it.
- **The bus keeps no history.** A late or reconnected subscriber gets only new
  segments; never present what was received as a complete transcript unless
  connected for the whole encounter.

### The nudge payload — one shape (cross-cutting)
What rides on `nudges:{session_id}`. TASK-040 publishes, TASK-041 relays verbatim,
TASK-042/043 render, TASK-044 publishes a second variety.

```json
{
  "type": "PAYER_RULE_ALERT",
  "nudge_id": "0b7f...",
  "procedure": "knee MRI",
  "cpt_code": "73721",
  "message": "Prior authorization required for knee MRI. Still undocumented: ...",
  "missing_criteria": ["six weeks of conservative therapy"],
  "denial_risk": "high",
  "haptic": false
}
```

| Field | Type | Notes |
|---|---|---|
| `type` | `str` | `PAYER_RULE_ALERT` today; clients switch on it (TASK-044 adds another). |
| `nudge_id` | `str` (UUID) | `clinical_nudges` PK, used by `PATCH /nudges/{nudge_id}/acknowledge`. The row is written **before** the publish. |
| `procedure` | `str` | As the clinician said it. |
| `cpt_code` | `str \| None` | **Nullable from the start** (TASK-044 nudges on keywords with no code). |
| `message` | `str` | From `gap_analysis.nudge_message()`. Procedure + payer criteria, nothing from `clinical_context`. |
| `missing_criteria` | `list[str]` | Empty on a fallback answer means *unknown*, not *none missing*. |
| `denial_risk` | `"low" \| "medium" \| "high"` | Drives TASK-042's yellow/orange/red banner. |
| `haptic` | `bool` | Buzz the device (TASK-043). **Not a synonym for `denial_risk == "high"`.** |

**`haptic` is a decision, not a restatement of `denial_risk`.** Rule:
`denial_risk == "high"` **and** the answer is not the safe fallback.
`query.fallback_answer()` returns `high` for an unreachable Qdrant, Bedrock error,
or empty retrieval; an outage must not buzz every physician's device and teach them
to ignore real high-risk alerts. The nudge still fires at `high`; only the
escalation is suppressed. Encode it explicitly in the emitter.

**Whether to nudge at all is decided in one place, and it is not this payload** —
see below.

### The nudge trigger is the message (corrects shipped TASK-012 logic)
**The bug:** TASK-040's trigger (`missing_criteria` non-empty or `denial_risk ==
"high"`) and `gap_analysis`'s message composition were two derivations of one
decision and disagreed in two silent cases: (1) auth required but no criteria found
(`medium`, empty list, yet a "confirm manually" message), and (2) step therapy only
(`_with_step_therapy_floor` lifts to `medium` with nothing missing).

**The fix: `gap_analysis` decides, and the message carries the decision.**
`nudge_message()` returns `str | None`; `None` means nothing worth interrupting for.
The emitter fires iff it has a message; no consumer re-derives the condition.
`PolicyQueryData.nudge_message` is nullable (in `docs/api/track-b-rag.yaml`).
Keying off "non-empty message" without making it nullable would nudge on every
query, since every branch used to return text.

`None` when auth is not required and no step therapy applies, or when auth is
required, criteria are known, and all are documented. Everything else carries a
message — including the safe fallback, which always nudges with `haptic`
suppressed. Shipped as its own bugfix commit with a regression test per silent case.

### Qdrant Initialization — Must Be Idempotent
Never call `recreate_collection()` in startup code — it wipes all indexed policies
on every restart. Use get-or-create:
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
`recreate_collection` is acceptable only in a dev reset script a human runs deliberately.

### Bedrock Model Assignment (concrete, per call site)
| Call site | Model | Env var | Why |
|---|---|---|---|
| TASK-012 policy query analysis | Sonnet | `BEDROCK_MODEL_ID_REASONING` | Multi-step reasoning over retrieved policy text |
| TASK-030 SOAP note generation | Sonnet | `BEDROCK_MODEL_ID_REASONING` | Long-form structured clinical writing |
| TASK-030 ICD-10/CPT extraction (LLM pass) | Haiku | `BEDROCK_MODEL_ID_FAST` | Extraction, not reasoning — validated against Comprehend Medical in TASK-031 |
| TASK-013 policy scraper (if any LLM cleanup used) | Haiku | `BEDROCK_MODEL_ID_FAST` | Simple text cleanup |

Use the `_FAST`/`_REASONING` env var names in code; never hardcode a model ID string.

### Extracted clinical codes — one JSON shape (cross-cutting)
`clinical_notes.icd10_codes` and `clinical_notes.cpt_codes` (JSONB) each hold a
**JSON array of objects**. Writers/readers: TASK-030 (writes), TASK-031 (validates
ICD-10), TASK-060 (bundle diagnoses), TASK-071 (renders/edits via TASK-032 `PATCH`).

```json
[
  {
    "code": "M17.11",
    "display": "Unilateral primary osteoarthritis, right knee",
    "source": "llm-extraction",
    "confidence": null,
    "validation": null
  }
]
```

| Field | Type | Notes |
|---|---|---|
| `code` | `str`, required | ICD-10-CM stored **dotted** (`M17.11`); CPT five characters. Uppercased, stripped, dot-normalised on write. |
| `display` | `str \| None` | The source's own description, never invented. |
| `source` | `"llm-extraction" \| "comprehend-medical" \| "provider-accepted"` | Which pass proposed it, or that a provider accepted it. |
| `confidence` | `float \| None`, 0.0–1.0 | Proposing source's score. **Always `None` for `llm-extraction`** (a model's self-rating is not a measurement) **and for `provider-accepted`** (a human acceptance is a fact). |
| `validation` | object \| `None` | Written by TASK-031. |
| `validation.source` | `"comprehend-medical"` | The validating pass. |
| `validation.confidence` | `float \| None` | Comprehend's **`ICD10CMConcept.Score`** for the matching code (never the entity-level `Score`, which measures detection, not linkage), or `None` if no such concept. |
| `validation.confirmed` | `bool` | Produced at or above TASK-031's 0.8 threshold. |

**Storage is dotted, matching is dotless, and both halves are one function.**
Sources disagree on dots (`M17.11` vs `M1711`); every cross-source comparison goes
through one shared dotless-key function. Never normalise independently.

- **`validation: null` means "not checked yet" and never "checked and rejected".**
- **CPT entries keep `validation: null` indefinitely** — Comprehend Medical has no
  CPT inference.
- **A `comprehend-medical` entry is a suggestion, not a stated diagnosis:**
  - keeps `validation: null` permanently (validating Comprehend against itself is
    circular);
  - TASK-071 renders it visibly as a machine suggestion;
  - TASK-060 never puts it in a bundle as a diagnosis;
  - **a code is one entry, whichever pass found it** — never append a duplicate;
    compare via the dotless key.
- **`provider-accepted` is the third source, and it is what a human acceptance
  looks like:**
  - **only TASK-032's `PATCH /notes/{session_id}` writes it**; no automated path
    may promote one;
  - `confidence` and `validation` are always `None`;
  - TASK-060 treats it like `llm-extraction` (claimable); TASK-071 renders it as
    accepted;
  - acceptance mutates the existing entry's `source` in place — never appends.
- **`null` and `[]` are different answers on these columns.** `[]` = extraction ran
  and found nothing (reconciliation runs); `null` = no answer produced
  (reconciliation does **not** run). See `GeneratedNote` in
  `services/track-a-clinical/src/track_a_clinical/soap.py`.
- **So an editing endpoint needs three states, not two**: field omitted, set to
  `null`, or a list. Use `exclude_unset` (or a sentinel), never a `None` default,
  and test the omitted-field case.

The Pydantic model lives in `services/track-a-clinical/src/track_a_clinical/models/`.

### An accumulated transcript exceeds downstream limits (cross-cutting)
`TranscriptBuffer` in track-a-clinical is unbounded by design — never truncate
clinical speech. Visits routinely produce tens of thousands of characters, so
**every consumer of a full transcript must state what it does when the transcript
exceeds the service it feeds.**
- **AWS Comprehend Medical `InferICD10CM` accepts at most 10,000 characters**
  (botocore `{'min': 1, 'max': 10000}`, server `TextSizeLimitExceededException`).
  Not the 20,000-byte `DetectEntitiesV2` figure. The async batch path is S3-based and
  unsuitable for live encounters.
- **Bedrock's context window** bounds SOAP/extraction; not binding today, same rule.

**The standing rule: chunk and merge, or report reduced coverage — never
silently truncate.** A consumer that cannot process the whole transcript must say
so in its output or operational log, naming what was left unexamined.

### Migration Ownership vs. Table Write Access (clarifies TASK-005)
"Owns the schema" means owns the Alembic migration history, not exclusive write
access. `clinical_nudges` is migrated by track-a-clinical but written by
track-b-rag; `prior_auth_requests` is migrated by track-a-clinical but written by
prior-auth. All services share one Postgres via `DATABASE_URL`.

### Where the shared SQLAlchemy models live (cross-cutting — applies to every task)
Mapped classes for shared tables live in
`services/track-a-clinical/src/track_a_clinical/models/`, one module per table,
exported from `__init__`. Every service imports them — never maps its own class:

```python
from track_a_clinical.models import ClinicalNudge, Encounter
```

That is why track-a-clinical builds `src/track_a_clinical/` rather than bare `src/`.
Services still declaring `packages = ["src"]` all install a top-level `src` that
shadows the others in the shared venv; each moves to a named package when it grows
code worth importing across the boundary (track-b-rag did so as `src/track_b_rag/`
in TASK-010).

### Payer and jurisdiction identity — one canonical vocabulary (cross-cutting)
**The bug this closes:** `payer` was matched by exact string in the Qdrant filter
(`track_b_rag/retrieval.py`) and the `rag:` key, with no normalisation, so
"Medicare Part B" never matched an ingested "CMS" — an empty retrieval
indistinguishable from "no policy held".

**The rule.** `payer` is a canonical slug everywhere it is stored, matched or
keyed, never a display name. Both sides use `packages/payer-vocab`:

```python
normalize_payer(raw: str) -> str   # "Medicare Part B" -> "cms-medicare"
is_known_payer(slug: str) -> bool  # False for a name the vocabulary has never seen
```

- **Deterministic slugging** — casefold, strip legal suffixes and punctuation,
  collapse whitespace, hyphenate.
- **An alias table handles what slugging cannot reach** ("Medicare", "Original
  Medicare", "CMS", … → `cms-medicare`). Curated data; extend the table rather than
  making slugging cleverer.
- **An unknown payer still queries, but says so** — WARNING when
  `is_known_payer()` is false.
- **Display names are kept** in the Postgres row for humans.
- **A payer family is not one payer.** BCBS's 33 licensees publish separate
  criteria: Anthem names → `anthem-bcbs`, held licensees get their own slug
  (`bcbs-ma` first), unqualified "Blue Cross"/"BCBS" → generic
  `blue-cross-blue-shield`. Seed and ingest under the publishing licensee's slug,
  never the generic one.
- **Extend the alias table from observed data, not from plausible spellings**
  (existing rows come from real `Coverage.payor.display` values on the Cerner
  sandbox, public HAPI, and Synthea). Non-carrier names (`SELF PAY`, `Government`,
  `Dual Eligible`) stay unmapped.

Consumers: `/policies/ingest` and `/policies/query` (TASK-011/012), policy scraper
(TASK-013), seed script (TASK-014), fhir-integration's Coverage → query (Phase 5).

**Jurisdiction is the same problem one column over.** Medicare LCDs apply per MAC
jurisdiction (median 12 states). CMS state codes include territories, the
4-character `CNMI`, and sub-state codes. Normalise to the parent state's USPS code
at ingestion: `CNMI` → `MP`, `DN`/`QN`/`UN` → `NY`, `NF`/`SF` → `CA`,
`EM`/`WM` → `MO`.

**A multi-state policy is one document with a list of states, never one copy per
state.** Qdrant's `MatchValue` matches any element of a list payload (verified with
`policy_query_filter`, with and without the keyword index), and the
`IsNullCondition` for national policies still works. Per-state copies would
multiply embedding cost and crowd `TOP_K=8`. See TASK-013 for the Postgres side.

### packages/api-envelope — Design Decisions (locked, do not revisit)
**Scope note:** the single definition of the response envelope plus the FastAPI
exception handlers that put FastAPI's own failures into it. **Not** a shared web
framework: no routes, auth, dependencies, or middleware.
- **Every service imports it; no service defines its own envelope.**
- **The validation handler never echoes a rejected value** — field *locations*
  only, since bodies carry PHI.
- **`error_responses()` carries generic per-status wording, overridable per
  route** via `descriptions={404: "..."}`; an undeclared status raises.
- **`GET /health` is the one documented departure**: a 503 returns `data` with the
  per-dependency flags and `error: null`. Health endpoints also write no audit row.

### packages/session-auth — Design Decisions (locked, do not revisit)
**Scope note:** the single definition of the session-token validation in "How the
JWT reaches a WebSocket endpoint" (two carriers, checks, 4401). It **validates and
never mints** — `POST /sessions/start` is the only issuer (Known Constraint 8). No
routes, dependencies or middleware.
- **Every real-time endpoint imports it; none writes its own validator** (two
  copies is how a parallel auth mechanism arrives by accident).
- **`validate_token()` takes a signing key, not a `Settings` object.**
- **The issuer keeps its own `JWT_ALGORITHM` and `MIN_SIGNING_KEY_BYTES`** in
  `track_a_clinical.config`; the issuer does not import this package.
  `tests/unit/test_issuer_contract.py` (in this package) feeds the real issuer's
  output to the validator and checks the key floors match.
  `.github/scripts/detect-changed-members.sh` selects this package when
  track-a-clinical changes.
- **Nothing here logs a token, a claim, or a reason containing either.** Refusal
  reasons are fixed labels (`expired`, `session_mismatch`, `malformed_claim`).

### packages/logging-policy — Design Decisions (locked, do not revisit)
**Scope note:** sets log-level floors for *third-party* loggers only. No handlers,
no format, no root logger, no repo-owned loggers; unrelated to hipaa-logger.

**The gap it closes (TASK-046):** `httpx` logs every request's **full URL** at INFO
(`Patient/{id}`, `Coverage?patient={id}`, `Patient?name=`) — PHI in stdout.

**Installed in every service** (unlike cors-policy), because every process can run
at DEBUG and links libraries that write request content there — including
audio-ingestion (botocore DEBUG carries clinical text) and policy-scraper.

**Floors, each chosen by running the library:**
- **`httpx` → WARNING** (it leaks at INFO).
- **`urllib3` → INFO** (full request line at DEBUG).
- **`botocore` and `boto3` → INFO** (full request/response bodies at DEBUG —
  transcripts and SOAP notes; `botocore.credentials` INFO is useful and harmless).
- **`httpcore` → INFO** — precautionary; it has never been observed leaking.

**`sqlalchemy.engine` is deliberately not in the table.** Its statement/parameter
logging is controlled by the engine's `echo` flag, not the logger level (verified
on 2.0.52), so a floor would protect nothing. No engine may pass `echo` against a
PHI database.

**It raises and never lowers** existing levels, and does not fight a later explicit
`setLevel`.

**Where it is called:** first in each service's `create_app()`, and in
`policy_scraper.__main__.main()` after `basicConfig`. Tests use `create_app()`, so
the policy is active in suites.

**Do not solve this class of problem by moving identifiers out of URLs** — FHIR
search needs them there. A new HTTP/AWS client dependency means checking what it
logs and adding a floor in the same change.

### packages/hipaa-logger — Design Decisions (locked, do not revisit)
**Scope note:** NOT a general application logger. It writes one compliance audit
row per PHI access to the `audit_log` table — metadata (who, what resource, when),
never PHI content. Normal logging uses `logging.getLogger(__name__)`.
**A route calls `audit_log()` if and only if it touches PHI** (Known Constraints #6).
Both directions matter: operational rows would make "who accessed patient X" a
filtered query instead of a plain one. Health probes and `POST /policies/ingest`
(public payer documents) do *not* audit; they log at INFO.
- **Owns its own audit_log table and Alembic migration**, in
  `packages/hipaa-logger/migrations/`, applied before any service migration.
- **Raw asyncpg, not SQLAlchemy** — a single hot-path INSERT; an intentional exception.
- **Self-managed lazy connection pool** from `DATABASE_URL`, with an injection hook
  (`set_connection(conn)` / optional `conn` param) for tests and for writing the
  audit row inside a caller's transaction.

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
    fhir_practitioner_ref VARCHAR(512),    -- an EHR-asserted actor that is not a UUID
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_audit_log_occurred_at ON audit_log(occurred_at);
CREATE INDEX idx_audit_log_actor ON audit_log(actor_id);
CREATE INDEX idx_audit_log_session ON audit_log(session_id);
CREATE INDEX idx_audit_log_fhir_practitioner ON audit_log(fhir_practitioner_ref);
```
`fhir_practitioner_ref` (TASK-051c) is **not a second spelling of `actor_id`** —
see "The EHR-asserted actor is its own column".

```python
async def audit_log(
    actor_id: str | None,
    action: AuditAction,  # the vocabulary, never a bare string — see below
    resource_type: str | None,
    resource_id: str | None,
    session_id: str | None,
    service_name: str,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    fhir_practitioner_ref: str | None = None,  # EHR-asserted actor — see below
    conn: asyncpg.Connection | None = None,  # injection hook — uses pool if omitted
) -> None: ...
```
`ip_address`/`user_agent` default to None until request-context middleware
populates them for routes.

`resource_type` is the resource the row is about (`Encounter`, `ClinicalNote`,
`ClinicalNudge`, `PriorAuthRequest`, `Patient`); `resource_id` is its primary key.

### Auditing work that no request triggered (cross-cutting — every consumer)
Redis-triggered work (TASK-030 SOAP generation, TASK-060 bundle assembly) reads and
writes PHI with no request behind it.
- **The "if and only if it touches PHI" test is unchanged — only the trigger
  is.** Consumers that touch PHI audit.
- **`actor_id` is `encounters.provider_id`, read from the encounter row.** Signals
  carry no identity. **Never mint a service-account UUID** — an honest null beats a
  fabricated actor.
- **`session_id` comes from the same row.**
- **`ip_address` and `user_agent` are permanently `None` here, not pending.**
- **One row per unit of work, never one per message** (one per generated note,
  not per transcript segment).
- **The audit row joins the transaction that does the work** via
  `audit_log(..., conn=...)`.

### Auditing a PHI read that happens before any encounter exists (cross-cutting)
At SMART launch (TASK-052, TASK-025b patient search, TASK-053) fhir-integration
reads PHI before any `encounters` row exists.
- **`actor_id` is `None`, and that is the honest answer rather than a gap.** Never a
  service-account UUID.
- **The correct source is the SMART `fhirUser` claim** from the verified
  `id_token` — a `Practitioner` reference. **It goes in `fhir_practitioner_ref`,
  not in `actor_id`.**
- **Capturing `fhirUser` was TASK-051c, and it is built**
  (`services/fhir-integration/src/smart/identity.py`): JWKS fetch, signature and
  issuer verification, `Practitioner` resolution; the reference is stored on the
  launch record for later reads.
- **Verification failure is never a launch failure.** Missing `id_token`, no keys,
  bad signature, foreign `aud`, or a non-`Practitioner` claim leave the actor
  unknown. **The unverified claim is never written** (a `Patient` reference doubly so).
- **`actor_id` stays `None` in this service permanently** — nothing is pending; do
  not leave comments naming TASK-051c as an open gap.
- **Rows written before TASK-051c carry neither identifier, and nothing
  backfills them.**
- **The read still audits.**

### The EHR-asserted actor is its own column (cross-cutting)
**Why:** `audit_log.actor_id` is `UUID` and `hipaa_logger._as_uuid()` raises on
anything else; FHIR ids are `[A-Za-z0-9\-\.]{1,64}` (HAPI issues `"1"`), so passing
`fhirUser` as `actor_id` would fail exactly when capture succeeded.

**The decision: a separate column, `fhir_practitioner_ref`.** Rejected, and will
look tempting again:
- **Widening `actor_id` to text** — weakens the UUID-joins-to-provider guarantee
  every other row relies on.
- **A practitioner-to-UUID mapping table** — out of scope for this column (see
  "Provider identity" below for the `encounters` side).

- **The column holds the reference verbatim**, normally an absolute URL
  (`https://ehr.example.com/fhir/Practitioner/abc-123`) — bare ids collide across
  EHRs. Hence `VARCHAR(512)`.
- **A value only ever reaches this column after verification.**
- **"Who accessed patient X" is now two columns, not one.** Query both `actor_id`
  and `fhir_practitioner_ref`; neither is a fallback for or populated from the other.
- **General rule:** when a column's type is a guarantee others depend on, don't
  relax it for a new producer — give a different kind of value its own field.

**The action vocabulary is `hipaa_logger.AuditAction`, and it is the only
definition** (TASK-045). Services import members; none declares its own string.
`audit_log()` takes `AuditAction`, so mypy and a runtime check reject invented
actions. (A doc table plus per-service constants drifted three times; collapse
duplication rather than detect drift.)
- **Adding an action means adding a member to `AuditAction`** in the same change as
  the code that writes it.
- **Members for unbuilt work are expected** (`READ_PATIENT`, `SUBMIT_PRIOR_AUTH`).
- **Which service writes which action is deliberately not written down** —
  `grep -rn "AuditAction.<NAME>"`.
- **The meanings live on the members**, in
  `packages/hipaa-logger/src/hipaa_logger/actions.py`.
- **Do not widen `audit_log`'s parameter back to `str`.**

### Provider identity — the registry that resolves an EHR practitioner (cross-cutting)
Settled by TASK-025b; TASK-070 follows it. Same type mismatch as above, but for
`encounters.provider_id` — which is a *key* read by TASK-030/060 audits, the note
routes and the nudge acknowledge join, so a sibling column would fork every reader.

**The decision: a `providers` table, and `provider_id` is a row in it.** One row per
verified `Practitioner` reference (stored verbatim, `UNIQUE`); resolution is
get-or-create.
- **`POST /providers/resolve` in `track-a-clinical` is the only way a row is
  created**; fhir-integration calls it over HTTP.
- **It is not a PHI route and writes no audit row** — logs at INFO.
- **The client never sees a practitioner reference** — `GET /fhir/launch-context`
  returns the resolved `provider_id`.
- **A launch whose actor was never verified resolves to no provider** — null
  `provider_id`, and a visit cannot be started. Never a placeholder row.

**Rejected alternatives:** **a UUIDv5 derived from the reference** (one-way — audit
actors no query can resolve, looking like real providers) and **widening
`encounters.provider_id` to text** (same reason as `actor_id`).

**No foreign key from `encounters.provider_id` to `providers.id`, deliberately**,
while `/sessions/start` accepts an unauthenticated `provider_id`. Add it when Phase 5
provider authentication makes the field server-supplied.

### Alembic version table isolation
Migrations from hipaa-logger and each service share one database; a shared default
`alembic_version` table corrupts migration state. Every Alembic setup sets a unique
`version_table` in its `env.py`:
```python
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    version_table="alembic_version_track_a_clinical",
)
```
Pattern: `alembic_version_{package_or_service_name_with_underscores}` (e.g.
`alembic_version_hipaa_logger`). Applies to every future Alembic setup.

### DATABASE_URL format — single env var, two consumers
`DATABASE_URL` uses SQLAlchemy form `postgresql+asyncpg://user:pass@host/db`.
hipaa-logger (raw asyncpg) strips the driver suffix on connect — no second env var:
```python
def _to_asyncpg_dsn(database_url: str) -> str:
    """SQLAlchemy-style URLs use postgresql+asyncpg://; raw asyncpg wants postgresql://"""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
```

### FHIR
- FHIR version: R4 (4.0.1)
- SMART on FHIR version: 2.0
- Local dev FHIR server: HAPI FHIR at localhost:8080 (Docker)
- FHIR resources used: Patient, Encounter, Condition, Coverage, MedicationRequest,
  DocumentReference, Claim, Bundle, ClaimResponse. `Bundle`/`ClaimResponse` are what
  Da Vinci PAS `Claim/$submit` exchanges (TASK-004b). Add to this list in the change
  that models a new resource. `packages/fhir-types` also models `Location` and
  `Organization` (read only through an encounter's site of care, TASK-052b).

### EHR Priority Order (do not deviate from this)
1. **Athenahealth** — first. Most accessible developer program, common in private
   orthopedic and dermatology practices. Sandbox: developer.athenahealth.com
2. **eClinicalWorks** — second. Large specialty presence. Sandbox: developer.eclinicalworks.com
3. **Modernizing Medicine (EMA)** — third. Targets dermatology and orthopedics.
4. **Cerner (Oracle Health)** — fourth. Faster certification than Epic. Sandbox: code.cerner.com
5. **Epic** — last. Hardest certification (6-12 months, needs a reference customer);
   only after paying customers on other EHRs. Sandbox: fhir.epic.com

### Adapter Architecture (mandatory — do not build single-EHR)
All EHR integration goes through the adapter layer in services/fhir-integration.
Never put EHR-specific logic in route handlers or other services.

```
services/fhir-integration/src/adapters/
├── base.py        # EHRAdapter — standard FHIR R4 / US Core, works on all EHRs
├── athena.py      # AthenaAdapter(EHRAdapter) — overrides prior auth (no FHIR PAS)
├── ecw.py         # ECWAdapter(EHRAdapter) — minor coverage field handling
├── modmed.py      # ModMedAdapter(EHRAdapter) — EMA-specific extensions
├── cerner.py      # CernerAdapter(EHRAdapter) — coverage fallback handling
├── epic.py        # EpicAdapter(EHRAdapter) — proprietary extension enrichment
└── factory.py     # get_adapter(ehr_type, fhir_base_url, access_token) -> EHRAdapter
                   # detect_ehr_from_issuer(iss_url) -> EHRType
```

The SMART launch `iss` identifies the vendor — pass it to `detect_ehr_from_issuer()`;
never hardcode EHR type.

**base.py has two layers of method:**
- **Primitives** (one US Core resource each, no composition): `get_patient(patient_id)`,
  `get_coverage(patient_id)` → `CoverageInfo`, `get_conditions(patient_id)`,
  `get_encounter(encounter_id)`.
- **Composed** (the override point): `get_patient_context(patient_id)` →
  `PatientContext`, built from the three patient primitives.
- Also on base.py: `write_clinical_note()` — DocumentReference write-back
  (TASK-053), taking one `ClinicalNoteContent`; **a subclass amends what
  `note_document.build_document_reference()` returns rather than rebuilding the
  resource** (the builder holds note type, US Core category and the outbound code
  filter). And `submit_prior_auth()` — Claim/$submit, Da Vinci PAS (TASK-054).

**Subclasses override the composed method (calling `super()`), not primitives**,
unless the deviation is genuinely in one fetch:
- Athena: `submit_prior_auth()` → CoverMyMeds API (no FHIR PAS)
- Epic: `get_patient_context()` → optional proprietary extension enrichment
- Cerner: `get_patient_context()` → coverage fallback if payer field incomplete

Rule: one-EHR code goes in a subclass; standard FHIR goes in base.py.

- **`EHRAdapter` is concrete and instantiable, never abstract** — it is what an
  unknown issuer gets.
- **An unrecognised issuer resolves to the base adapter and logs a WARNING — it
  does not raise.** Log the issuer host only, never the full URL.
- **`ehr_type` is a closed vocabulary — `EHRType`, a `StrEnum` — never a bare
  `str`** (it round-trips through Redis in `fhir_token:{launch_id}`).
- `detect_ehr_from_issuer()` matching: **case-insensitive; match the host, not the
  whole URL** (avoid "Epicenter Orthopedics" → Epic); **`"cerner"` and
  `"oraclehealth"` both mean Cerner, checked in that order**; remaining checks
  most-specific-first, with a test for any host matching two patterns.

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
`@mohamedbouchtout` owns everything, with explicit entries for
`/infrastructure/terraform/environments/production/`, `/docs/compliance/` and
`/packages/hipaa-logger/`. See the file.

### .github/workflows/ci.yml
Triggers on pull_request to main and push to main.
1. `detect-changes` — finds which services/packages changed
2. Per-member test jobs (only if changed): `ruff check` + `ruff format --check`,
   `mypy src/`, `pytest tests/ --cov=src --cov-fail-under=80`
3. `security-scan` — `bandit -r . -ll` on changed Python services
4. All jobs must pass before merge

Path filter groups (each maps to a test job):
- Python packages, each with its own job: `hipaa-logger`, `api-envelope`,
  `session-auth`, `logging-policy`, `crypto-utils` (`packages/<name>/**`).
- `fhir-types`: runs pytest **and** `tsc --noEmit` against
  `packages/fhir-types/typescript/` (its own npm workspace, so tsc catches drift
  from the Pydantic models).
- TypeScript packages `audio-wire`, `session-client`, `fhir-client`: `tsc --noEmit`
  + Vitest, and a change **also sets the `web` and `mobile` filters** (both apps
  compile their source).
- Services `track-b-rag`, `track-a-clinical`, `audio-ingestion`, `fhir-integration`,
  `nudge-service`, `prior-auth`, `policy-scraper`: `services/<name>/**` or `packages/**`.
- `web`: apps/web/** · `mobile`: apps/mobile/**

Rules:
- **Any directory under packages/ needs its own path-filter entry AND its own test
  job** — re-running dependent services is not a substitute. The 80% gate applies.
- **A service's OpenAPI spec selects that service's job**: `docs/api/<service>.yaml`
  selects `services/<service>` (the spec is half of `test_openapi_contract.py`'s
  contract). Keep the filename convention.
- **A test that guards two things must be re-run when either of them moves** — e.g.
  track-a-clinical changes select audio-ingestion and session-auth for the JWT
  contract test; track-b-rag and policy-scraper `src/` changes select html-digest,
  whose agreement test proves both still compute one policy digest (TASK-009).
- **The selection rules live in `.github/scripts/detect-changed-members.sh`** (pure
  function: paths on stdin, outputs on stdout), tested by
  `.github/scripts/detect-changed-members.test.sh` — add a case with every rule change.
- **A change under `.github/scripts/` selects every member.**
- **The `detect-logic` job is unconditional and declares no `needs`.**
- **Every job must appear in `ci-passed`'s `needs`** — it is the merge gate.

### .github/workflows/deploy-dev.yml
A `workflow_dispatch`-only stub during Phases 0-5; enabled in Phase 6.

### .github/workflows/nightly-live-checks.yml — gated tests actually run
Tests against live external sources (CMS Medicare Coverage Database, TASK-013;
later payer/EHR sandboxes) sit behind an env-var gate, skipped by default.
**A gated test must be paired with a scheduled run that turns the gate on** —
otherwise the gate is a deletion. This workflow runs nightly plus
`workflow_dispatch`.
- The gate defaults off.
- The job name names the external dependency.
- A failure is a real signal — fix code or fixtures; never relax the assertion.
- Only genuine external dependencies qualify; never use it to escape per-PR CI.

### .github/PULL_REQUEST_TEMPLATE.md
Enforces task linkage (`Closes TASK-XXX`), what changed, test evidence, and a HIPAA
checklist (no PHI in logs/errors, `audit_log()` on new PHI access, no secrets, no
audio on disk, new env vars in `.env.example`). Fill it in; see the file.

### .github/ISSUE_TEMPLATE/task.md
Task ID, phase, what to build, acceptance criteria and notes copied from TASKS.md.

### .github/dependabot.yml
Lives in `.github/` (not `workflows/`). **The file itself is authoritative.**
Weekly on Mondays, `chore(deps)` prefix, five ecosystems: `uv` (`/`, the whole
workspace — one entry, not per-directory `pip`), `npm` (`/apps/web`), `npm`
(`/apps/mobile`), `github-actions` (`/`), `docker-compose` (`/`). Terraform is
commented out until `.tf` files exist.
- Minor/patch grouped per ecosystem; majors individually.
- **Majors ignored:** `pydantic`, `sqlalchemy`, `langchain*`, `expo`,
  `react-native` (coordinated migrations), and **`postgres`** — v18 moved its data
  directory, breaking existing local volumes in a way CI (fresh volumes) cannot
  catch. Upgrading is a task: move the mount to `/var/lib/postgresql` and plan a
  `pg_upgrade` or reset.

### Backing service versions live in exactly one file
`docker-compose.yml` is the single source of truth for postgres, redis and qdrant.
**CI does not declare its own service containers** — it runs
`docker compose up -d --wait postgres redis qdrant`. Dependabot cannot watch Actions
service containers (dependabot-core#5819), so a second pin drifts silently. Add
any future backing service to `docker-compose.yml`; never add a `services:` block
to `ci.yml`.

### .github/ISSUE_TEMPLATE/bug_report.md
**Never paste PHI into an issue** — GitHub is not HIPAA-eligible. Use synthetic
values or a Synthea patient ID. Ticking the "Impact" PHI-exposure box means
notifying the security owner directly.

### packages/crypto-utils — Design Decisions (locked, do not revisit)
**Scope note:** field-level AES-256-GCM encryption with a KMS-wrapped DEK per
record, for specific sensitive fields. Not a replacement for encryption at rest,
not a general crypto toolkit.
- **Never log plaintext.** Plaintext DEKs and field values never reach a log line,
  exception message, or stack trace; errors name the field/context only. Enforced
  inside the primitives.
- **Encryption context is bound in two places, not one**: as the KMS encryption
  context *and* as AES-GCM AAD, so ciphertext swapped onto another record fails the
  GCM tag even if the KMS check were bypassed.
- **Moto (`@mock_aws`) for all KMS mocking in tests** — never hand-rolled
  `unittest.mock` on boto3.

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

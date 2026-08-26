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
│   ├── fhir-types/       # Shared FHIR R4 type definitions (Python + TypeScript)
│   ├── audio-wire/       # Encounter-audio wire format — both frontends (TypeScript)
│   └── crypto-utils/     # AES-256 helpers used across services
├── infrastructure/
│   ├── terraform/        # AWS infrastructure as code
│   └── kubernetes/       # K8s manifests + Helm chart
├── scripts/
│   ├── seed-synthea.sh   # Load synthetic patients into local HAPI FHIR (stub until TASK-052)
│   └── setup-dev.sh      # One-command dev environment setup (stub until TASK-052)
└── docker-compose.yml    # Full local stack — postgres, redis, qdrant, hapi-fhir, crd
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
- **Framework:** React 19 + TypeScript. Earlier drafts of this file said React
  18, written before `apps/web` was scaffolded; TASK-023 scaffolded it on 19
  because `apps/mobile` is already on 19.2.x through Expo SDK 57, and two React
  majors in one npm workspace root is a cost with nothing to buy — there was no
  existing web code to migrate.
- **Build:** Vite
- **Styling:** Tailwind CSS
- **State:** Zustand — not installed until a task has state to keep in it.
- **FHIR:** fhirclient.js for SMART on FHIR OAuth flow
- **WebSocket:** native WebSocket API (no socket.io)
- **Audio:** `getUserMedia` → `AudioContext({ sampleRate: 16000 })` →
  `AudioWorkletNode`, with the float-to-int16 conversion and 250ms framing in
  `packages/audio-wire`. **Not MediaRecorder**, which cannot emit raw PCM at
  all, offers no sample-rate control, and whose `timeslice` chunks are not
  independently decodable — measured, see TASK-023.
- **Testing:** Vitest + React Testing Library

### Frontend (apps/mobile)
- **Framework:** React Native with Expo SDK 57
- **Audio:** `expo-audio`'s `useAudioStream` for capture — real-time PCM buffers
  delivered to an `onBuffer` callback, never a recorded file. Earlier drafts of
  this file and of TASK-022 said `expo-av`; that was written without checking
  it, the same way the Node.js `fhir-integration` line was. `expo-av` records to
  a file URI and exposes no PCM callback at all, so it could satisfy neither the
  "audio never persists" constraint nor the 16kHz-PCM one — and it was removed
  from Expo entirely in SDK 55. The SDK pin moved from 51 to 57 because
  `useAudioStream` landed in SDK 56; `apps/mobile` is unscaffolded, so this
  costs nothing to adopt. See TASK-022 for the capture details.
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
      Container listens on 8090; published as 8006 because Windows reserves
      the 8081-8180 range and the container cannot bind 8090 there.
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
- Every commit message should have my sign-off at the end co-authered by you right
  you contributed to the commit.

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
- **Cache the payer-policy half of RAG results in Redis** with 24h TTL keyed by
  `rag:{payer}:{plan_type}:{state}:{cpt_code}`. This is a major cost lever —
  implement it from the start. **Cache only the payer-policy fields, never the
  patient-specific ones.** `/policies/query`'s response mixes two kinds of data:
  `requires_auth`, `auth_criteria`, `step_therapy_required`, and
  `step_therapy_details` are properties of the payer's policy and are identical
  for every patient with that payer/plan/state/CPT — those are what the key
  above caches. `missing_criteria`, `denial_risk`, and `nudge_message` are
  properties of *this patient's documentation* and differ per encounter;
  caching them across patients would serve patient B the gaps computed for
  patient A. Run that comparison fresh on every call against the cached policy
  rules. The expensive work (Qdrant search + Sonnet reasoning over retrieved
  policy text) is the cacheable half, so this preserves the full cost benefit.
  Adding `clinical_context` to the key instead would be correct but would
  collapse the hit rate to near zero, defeating the point.
- **Haiku for extraction tasks, Sonnet for reasoning.** ICD/CPT entity extraction → Haiku. SOAP generation and payer policy analysis → Sonnet. Costs 15x less per extraction call.
- **Policy lookup is two-tier, and the tiers answer different questions.** For
  payers covered by the CMS-0057-F mandate (Medicare Advantage, Medicaid managed
  care, CHIP, ACA marketplace), `/policies/query` (TASK-012) asks the payer's own
  Da Vinci **CRD** endpoint (TASK-015) *and* runs the RAG/Qdrant/Sonnet path
  (TASK-010–014), concurrently. CRD decides `requires_auth`, which it states
  directly and authoritatively; the RAG path supplies `auth_criteria` and the
  step therapy fields. Commercial employer-sponsored plans — the bulk of what
  private practices see — are not covered by the mandate and take the RAG path
  alone. Both arrangements return the same response shape; callers never branch
  on which one answered.
  **CRD does not carry the criteria, and this is a property of the standard, not
  of any one implementation.** The IG's `ext-coverage-information` extension
  carries `covered`, `pa-needed`, `doc-needed`, `doc-purpose`, `questionnaire`
  and assorted trace fields — nothing holding criterion text. CRD answers
  *whether* authorization and documentation are needed and delegates *what must
  be documented* to a DTR Questionnaire. So a CRD-only answer would hand Stage 2
  an empty criteria list and a nudge that cannot say what is missing, which is
  most of the product. Earlier drafts said CRD would let us "skip the RAG path
  entirely"; that was written before anyone ran a CRD server, and it is wrong.
  Da Vinci **DTR** is deferred to a later task — it needs a SMART on FHIR app
  surface that does not exist before Phase 5, and its Questionnaire items are
  largely administrative form fields (name, NPI, signature) that would poison
  the Stage 2 matcher if mapped into `auth_criteria` as they are.
  **Silence from a payer is never "no authorization required."** An empty card
  list, a card that only reports documentation requirements, and the
  "unable to process" card a payer returns when its rule needs more than we sent
  all mean *no determination*, and the RAG path then answers alone. Reading any
  of them as a negative determination would tell a provider an unauthorized
  order is clear — the one direction TASK-012 forbids failing in.
  **The CRD request carries no patient.** Stage 1 holds none by construction, so
  the request is built from payer, plan type, state and procedure code with a
  placeholder subject. CRD is specified as a patient-specific coverage check, so
  a payer rule keyed on age or sex simply cannot answer us — it returns "unable
  to process" and RAG answers instead. Fabricating a patient to make such a rule
  respond would produce a confident determination about someone who does not
  exist. Closing that gap is TASK-059, and it is gated on TASK-052 supplying
  real `Patient` and `Coverage` resources. Note what changes when it lands: a
  patient-carrying CRD request is a PHI disclosure to a third party, so TLS
  stops being a deployment convention, the endpoint has to be verified per
  payer rather than read from one `CRD_BASE_URL`, and the disclosure needs its
  own audit row. None of that applies to the patient-free request built today.
- **A CRD answer is never cached; a RAG answer is.** The `rag:` key exists
  because a Qdrant search plus a Sonnet call is expensive and its result is
  identical for every patient on that payer/plan/state/CPT — a day of staleness
  is an accepted trade for that. CRD is a different kind of answer: its entire
  value over RAG is being live and authoritative from the payer's own system at
  the moment of the order, so caching it for 24h discards the only property that
  justifies the second tier. The CRD path neither reads nor writes the `rag:`
  key. If CRD latency ever threatens the nudge budget, the answer is a short
  seconds-scale TTL of its own, never the 24h payer-policy key.

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
  a provider taps "start visit." It also publishes the new `session_id` to the
  fixed `sessions:started` channel — added in TASK-021, because a consumer of
  `transcription:{session_id}` has to learn a session exists before it can
  subscribe to that channel by name, and the alternative is a wildcard
  subscription across the channels carrying speech. The publish precedes the
  response, so the JWT a client needs to open its audio socket cannot exist
  before a consumer is listening. A failed publish is a 503 and the session is
  not usable: an encounter nobody watches raises no nudges and looks exactly
  like an encounter with nothing to flag.
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

**A visit outlasting the token re-mints; the encounter never ends because a
token expired.** `SESSION_TTL_SECONDS` is 15 minutes and a real orthopedic or
dermatology encounter routinely runs longer, so this is an ordinary case rather
than an edge one. It is settled here, once, because both session screens hit it
identically — TASK-025 on mobile and TASK-070 on web — and two apps each
deciding it alone is how they end up disagreeing. Both cite this section;
neither re-derives it.

- **The token bounds connection establishment, not stream lifetime.** Every
  real-time endpoint validates the JWT once, before completing the handshake,
  and never re-validates a connection that is already open — see
  `audio_stream()` in `services/audio-ingestion/src/api/websocket.py`, where
  `_authenticate` runs ahead of `accept()` and nothing in the receive loop
  revisits it. So an audio socket opened at minute 0 keeps streaming at minute
  40. Expiry only bites when a *new* socket must be opened: a reconnect after a
  drop, or the nudge socket (TASK-041) opening later than the audio socket.
- **Re-mint for the same `session_id`. Never by calling `POST /sessions/start`
  again.** A second start creates a second `encounters` row with a new
  server-generated `session_id`, which forks one visit into two encounters:
  the transcript splits across two `transcription:{session_id}` channels,
  TASK-030 generates two partial SOAP notes from two partial buffers, TASK-060
  assembles a bundle from whichever half it saw, and the `procedure_seen:` set
  no longer dedups across the visit, so one procedure raises a nudge twice.
  Nothing errors anywhere along that path — it is the failure this bullet
  exists to prevent, and it is exactly the shortcut a client reaches for when
  the only endpoint it has is `/sessions/start`.
- **The endpoint that re-mints without starting a session is TASK-006b**, and it
  does not exist yet. Until it lands, a client that needs a socket after `exp`
  has no correct move: it must surface `AUTH_REJECTED` and let the provider end
  the visit and begin a new one. That is a poor experience and an honest one —
  strictly better than silently forking the encounter. Do not work around the
  gap by re-calling `/sessions/start`.
- **Once TASK-006b exists, refresh proactively and reactively**: before opening
  any new socket when the held token is close to `exp`, and on `AUTH_REJECTED`
  from a socket that failed to open. Clients already hold `exp` — a claim in
  the token they were given — so the proactive check costs nothing.
- **A refreshed token does not extend the encounter.** The encounter ends when
  the provider ends it, via `POST /sessions/{session_id}/end`. Token lifetime
  and visit lifetime are independent, and conflating them is what produced the
  question in the first place.

**How the JWT reaches a WebSocket endpoint — either carrier, never both
required.** A WebSocket endpoint accepts the session token in *either* of two
places, and one is enough:

```
Authorization: Bearer <jwt>                         # header carrier
Sec-WebSocket-Protocol: medauth.session.v1, medauth.jwt.<jwt>   # subprotocol carrier
```

The header is the obvious carrier and it is what service-to-service callers and
tests use. It is not available to a browser: the native `WebSocket` constructor
takes a URL and a subprotocol list and nothing else, and `apps/web` is required
by the Frontend section above to use the native API rather than a library that
tunnels its own handshake. That is a platform constraint, not an implementation
gap to route around, so the second carrier exists for it. Every real-time
endpoint therefore supports both, and this is the canonical mechanism rather
than something TASK-020 settled locally: TASK-041's nudge socket inherits it by
reference, and TASK-023's browser capture has to send the subprotocol form
because nothing else is open to it.

Rules that make the two carriers behave identically:
- **Validation is the same whichever carrier was used** — signature against
  `JWT_SIGNING_KEY`, `exp` in the future, and the token's `session_id` claim
  equal to the `session_id` in the URL path. Where the token arrived from is not
  an input to any of those checks.
- **Reject before the handshake completes.** Validation runs before the
  connection is accepted, never after, so an unauthenticated peer never reaches
  a state where it can send a frame. The close code is 4401.
- **The server echoes `medauth.session.v1` and never the token.** A browser
  aborts a connection whose handshake response does not name one of the
  subprotocols it offered, so the accept must select one — and selecting the
  `medauth.jwt.` entry would write the credential into the response headers, and
  from there into every proxy access log on the path. Offer the version marker
  first for exactly this reason: it gives the server something safe to echo.
- **A token carried this way is still a credential.** It is never logged, never
  put in an error message, and never placed in the URL query string, which is
  the third thing browsers can carry and the one place a credential is certain
  to be logged by intermediaries. The 15-minute `SESSION_TTL_SECONDS` lifetime
  bounds the damage; TLS is what actually protects the handshake.

Note what a close code cannot do. Below the ASGI layer, a connection refused
*before* the handshake completes has no WebSocket frame to carry a code in, so a
real server answers the upgrade request with an HTTP status — the 4401 is what
the application emits and what an ASGI-level test observes, and a browser client
sees a failed upgrade rather than an `onclose` with 4401. This is the correct
trade: accepting an unauthenticated handshake purely so the rejection reads
nicely is worse than the client having to distinguish a failed upgrade from a
normal close.

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
                                 track-a-clinical itself (TASK-030),
                                 prior-auth (TASK-060) and track-b-rag's
                                 transcript consumer (TASK-021)
sessions:started                 pub/sub — the one fixed channel here, carrying
                                 {"session_id": ...} as its payload because the
                                 channel name has no room for it. Published by
                                 track-a-clinical (TASK-006), consumed by
                                 track-b-rag (TASK-021). It exists so a
                                 consumer can subscribe to one session's
                                 transcript channel by name; the alternative
                                 was pattern-subscribing transcription:*, a
                                 wildcard over the channel family that carries
                                 speech. Published before /sessions/start
                                 returns the JWT, so a consumer is always
                                 listening before the client can open its
                                 audio socket.
procedure_seen:{session_id}      set, 4h TTL — the procedure keys already
                                 queried during one encounter, so a procedure
                                 named three times raises one nudge and not
                                 three (TASK-021). Members are `cpt:{code}`
                                 where a CPT code resolves and
                                 `keyword:{keyword}` where none does
                                 (TASK-024), so two keywords naming one
                                 procedure share a claim — a knee MRI and a hip
                                 MRI are both 73721 and are one order. Claimed
                                 with SADD, which reports first-add atomically;
                                 deleted on session:ended, with the TTL only
                                 bounding a visit that never ends.
rag:{payer}:{plan_type}:{state}:{cpt_code}
                                  cache, 24h TTL — payer-policy fields ONLY
                                  (requires_auth, auth_criteria,
                                  step_therapy_required, step_therapy_details).
                                  Never the patient-specific fields
                                  (missing_criteria, denial_risk,
                                  nudge_message) — those are recomputed per
                                  call. See the cache note in Key
                                  Architectural Constraints. (TASK-012)
fhir_session:{state_param}       cache, TTL = OAuth flow timeout (~10 min) —
                                  transient SMART launch state (TASK-051)
fhir_token:{session_id}          cache, TTL = token expiry — EHR access token
                                  + fhir_base_url + ehr_type (TASK-051)
```
Lowercase, colon-separated, most-specific segment last. If a task needs a new
Redis key pattern not listed here, add it to this list in the same PR.
The `{payer}` segment is the canonical slug from `packages/payer-vocab`, never a
payer's display name — see "Payer and jurisdiction identity" below for why a raw
name in this key silently halves the hit rate and hides a retrieval miss.

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
| Call site | Model | Env var | Why |
|---|---|---|---|
| TASK-012 policy query analysis | Sonnet | `BEDROCK_MODEL_ID_REASONING` | Multi-step reasoning over retrieved policy text |
| TASK-030 SOAP note generation | Sonnet | `BEDROCK_MODEL_ID_REASONING` | Long-form structured clinical writing |
| TASK-030 ICD-10/CPT extraction (LLM pass) | Haiku | `BEDROCK_MODEL_ID_FAST` | Extraction, not reasoning — validated against Comprehend Medical in TASK-031 anyway |
| TASK-013 policy scraper (if any LLM cleanup used) | Haiku | `BEDROCK_MODEL_ID_FAST` | Simple text cleanup, not analysis |
The actual `.env.example` vars are `BEDROCK_MODEL_ID_FAST` and
`BEDROCK_MODEL_ID_REASONING` — an earlier draft of this table named them
`BEDROCK_MODEL_SONNET`/`BEDROCK_MODEL_HAIKU`, which never matched the repo.
Fixed here; use the `_FAST`/`_REASONING` names in code, never hardcode a
model ID string.

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
grows code worth importing. **track-b-rag is that case as of TASK-010**: it
crosses the boundary one task later (TASK-011 imports the shared SQLAlchemy
models from `track_a_clinical.models` to write the `insurance_policies` row).
Rename to `src/track_b_rag/` and declare `medauth-track-a-clinical` as a
dependency in TASK-010, while the service is still empty — cheaper now than
as churn after TASK-011 through TASK-015 exist.

### Payer and jurisdiction identity — one canonical vocabulary (cross-cutting)
**The bug this closes.** `payer` is matched by exact string equality in two
places: Qdrant's retrieval filter (`FieldCondition(key="payer",
match=MatchValue(value=payer))` in `track_b_rag/retrieval.py`) and the Redis
cache key `rag:{payer}:{plan_type}:{state}:{cpt_code}`. Nothing normalises it on
either side — ingestion stores whatever string the uploader sent, and the query
endpoint filters on whatever string the caller sent. `state` and `cpt_code` are
at least uppercased; `payer` is not touched at all. At query time that string
originates in a FHIR `Coverage` resource's free-text payer display: "Medicare
Part B", "AETNA", "Aetna Better Health of MA". None of those equals the "CMS" or
"Aetna" an ingest wrote. The failure is silent — retrieval returns zero chunks,
the RAG path reports it found no policy, and that is indistinguishable from a
payer we genuinely hold no policy for.

**The rule.** `payer` is a canonical slug everywhere it is stored, matched or
keyed, and never a display name. Both sides call the same function, from
`packages/payer-vocab`:

```python
normalize_payer(raw: str) -> str   # "Medicare Part B" -> "cms-medicare"
is_known_payer(slug: str) -> bool  # False for a name the vocabulary has never seen
```

- **Deterministic slugging is the mechanism** — casefold, strip legal suffixes
  and punctuation, collapse whitespace, hyphenate. It guarantees two spellings
  of one name cannot become two payers.
- **An alias table handles what slugging cannot reach.** "Medicare", "Medicare
  Part A", "Medicare Part B", "Original Medicare" and "CMS" all resolve to
  `cms-medicare`. This is curated data, not inference — extend the table when a
  new payer appears rather than making the slug function cleverer.
- **An unknown payer still queries, but says so.** A payer the vocabulary has
  never seen is not an error; it gets a slug and runs. The query path logs at
  WARNING when `is_known_payer()` is false, so "the name did not line up" is
  visible in the operational trace instead of looking like "no policy found".
  That distinction is the whole point of this section.
- **Display names are not discarded** — keep the payer's own spelling in the
  Postgres row for humans to read. Slugs are for matching, not for display.
- **A payer family is not one payer.** The Blue Cross Blue Shield Association
  licenses 33 independent companies that each publish their own
  prior-authorization criteria, so the vocabulary keeps them apart: Anthem-branded
  names resolve to `anthem-bcbs`, a licensee we hold policies for gets its own
  slug (`bcbs-ma` is the first, Massachusetts being the pilot geography), and an
  unqualified "Blue Cross" or "BCBS" lands in the generic `blue-cross-blue-shield`
  bucket. Collapsing them would let one licensee's ingested policy answer a query
  about another — a wrong answer served silently, which is strictly worse than
  the empty retrieval plus WARNING that this package exists to produce. Seed and
  ingest under the publishing licensee's slug, never the generic one.
- **Extend the alias table from observed data, not from plausible spellings.**
  The rows for `Coventry Healthcare`, `Cigna Health`, `Medi-Cal` and
  `Humana Medicare Advantage` are there because those exact strings appear in
  `Coverage.payor.display` on real servers — the Oracle Health (Cerner) open
  sandbox, the public HAPI R4 server, and Synthea's `insurance_companies.csv`,
  which is what the local dev HAPI server is seeded from. Names that describe no
  carrier at all (`SELF PAY`, `Government`, `Dual Eligible`) are deliberately
  left unmapped: giving them a slug would manufacture a payer identity the source
  never asserted.

It is a package rather than a module inside track-b-rag because the consumers
span services: `/policies/ingest` and `/policies/query` (TASK-011/012), the
policy scraper (TASK-013), the seed script (TASK-014), and fhir-integration when
it turns a `Coverage` resource into a query (Phase 5). Same reasoning as
api-envelope, and the `rag:` cache key's `{payer}` segment is a canonical slug
for the same reason.

Doing this after TASK-014 seeds a corpus would mean re-ingesting that corpus.
Doing it now costs a re-run of a dev-only index. Same argument as TASK-010's
package rename.

**Jurisdiction is the same problem one column over.** A Medicare LCD is issued
per Medicare Administrative Contractor jurisdiction and applies in every state
that jurisdiction covers — a median of 12 states across the 949 current LCDs,
and 48 for the widest. CMS's own state vocabulary is *not* a list of USPS state
codes: it carries territories, a four-character `CNMI` that will not fit
`CHAR(2)` at all, and sub-state jurisdictions — `DN`/`QN`/`UN` (New York
downstate, Queens, upstate), `NF`/`SF` (northern and southern California) and
`EM`/`WM` (Missouri). Writing any of those into a `state` that a FHIR `Coverage`
will later be matched against reproduces the payer bug exactly. Normalise to the
two-character USPS code of the parent state at ingestion time (`CNMI` → `MP`,
`DN`/`QN`/`UN` → `NY`, `NF`/`SF` → `CA`, `EM`/`WM` → `MO`).

**A multi-state policy is one document with a list of states, never one copy per
state.** Qdrant's `MatchValue` matches any element of a list-valued payload
field — verified against the running Qdrant using the exact filter in
`policy_query_filter`, with and without the keyword payload index TASK-011
creates. So a payload of `state: ["MA", "ME", "NY"]` needs *no* change to the
retrieval filter, and the `IsNullCondition` that lets national policies match
every state keeps working alongside it. Copying a policy per state would instead
duplicate identical text a median of 12 times in Qdrant — 12× the embedding
cost, and near-duplicate chunks crowding each other out of `TOP_K=8`. See
TASK-013 for the Postgres side of the same decision.

### packages/api-envelope — Design Decisions (locked, do not revisit)
**Scope note (read first):** this package is the single definition of the HTTP
response envelope fixed in the API Design section above — `{"data": ..., "error":
null}` and `{"data": null, "error": {...}}` — plus the FastAPI exception handlers
that put FastAPI's own failure paths into it. It is **not** a shared web
framework: no routes, no authentication, no dependencies, no middleware. A
service's domain surface stays in that service.
- **Every service imports it; no service defines its own envelope.** It was
  extracted in TASK-010, when `track-b-rag` became the second consumer and
  started as a copy of `track-a-clinical`'s. Two hand-maintained definitions of
  a cross-service contract drift apart, for exactly the reason given above for
  centralising the shared SQLAlchemy models. Any service added later imports
  from here — copying it again is the thing this package exists to prevent.
- **The validation handler never echoes a rejected value.** FastAPI's
  `RequestValidationError.errors()` reports the offending field *and can include
  what was sent*, and request bodies in this monorepo carry patient identifiers
  and clinical context. The handler reports field *locations* only. This is a
  HIPAA constraint living inside the primitive, not a rule call sites are
  trusted to remember.
- **`error_responses()` carries generic per-status wording, overridable per
  route.** Pass `descriptions={404: "..."}` when a route's failure means
  something more specific than the default; an undeclared status raises rather
  than publishing a spec with an invented description.
- **`GET /health` is the one documented departure from the envelope's failure
  half.** A 503 from a health endpoint returns `data` populated with the
  per-dependency flags and `error: null` — the request succeeded, the answer is
  "unhealthy", and moving the flags into the error half would discard the only
  diagnostic the endpoint has. See the hipaa-logger scope note below for why
  the same endpoint also writes no audit row.

### packages/hipaa-logger — Design Decisions (locked, do not revisit)
**Scope note (read first):** this package is NOT a general application logger.
It writes one specific thing — a compliance audit trail row per PHI access —
to the audit_log Postgres table. It does not replace standard Python `logging`,
does not handle debug/info/error output, and is not used for anything except
PHI access events. Normal application logging (service startup, errors, request
traces) uses `logging.getLogger(__name__)` per the Python conventions section
above and goes to stdout/CloudWatch, not this package. Neither one ever logs
actual PHI content — hipaa-logger records metadata about an access (who, what
resource type, when), never the patient data itself. **A route calls
`audit_log()` if and only if it touches PHI** (Known Constraints #6 in
TASKS.md). This is the rule itself, not a default with exceptions bolted on:
an earlier phrasing required the call on *every* route, which immediately
needed a carve-out for `/health` and was about to need a second one for
`POST /policies/ingest`.

The reason the rule is an "if and only if" in both directions: the audit_log
table's value comes from every row in it being a PHI access. Mix operational
writes in and "who accessed patient X" stops being a query you can just run and
becomes one you have to filter. So a route over public or operational data must
*not* audit — health and liveness probes touch no PHI and auditing a k8s probe
on its polling interval is noise; `POST /policies/ingest` (TASK-011) writes
insurance policy documents, which are public payer publications with no patient
linkage. Those routes log at INFO through `logging.getLogger(__name__)` instead,
which still gives the operational trace, in the right place.
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
- `api-envelope`: packages/api-envelope/**
- `crypto-utils`: packages/crypto-utils/**
- `fhir-types`: packages/fhir-types/** — this job runs BOTH checks: pytest against
  the Pydantic models AND `tsc --noEmit` against packages/fhir-types/typescript/.
  It is the only package with two languages in one job. The TypeScript side is
  its own npm workspace (see TASK-004) so tsc actually catches drift between
  the Pydantic models and their TS mirrors, not just compiles them in isolation.
- `audio-wire`: packages/audio-wire/** — TypeScript only, so it runs `tsc
  --noEmit` and Vitest rather than joining the Python matrix. A change here also
  sets the `web` and `mobile` filters, because the package ships source that
  both apps compile into themselves rather than a built artifact.
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

### .github/workflows/nightly-live-checks.yml — gated tests actually run
Some tests depend on a live external source: the CMS Medicare Coverage Database
(TASK-013), and later the payer and EHR sandboxes. Those belong out of the
per-PR suite — an unrelated pull request should not go red because a government
site is down or a vendor sandbox is being rebuilt — so they sit behind an
environment-variable gate and are skipped by default.

**A gated test must be paired with a scheduled run that turns the gate on.**
This workflow runs nightly on `schedule:` with the gate set, plus
`workflow_dispatch` for running it by hand. Without it the gate is not a
deferral, it is a deletion: nothing ever executes the test, and drift in the
external source surfaces whenever someone happens to flip the flag, which is to
say at random. A scheduled failure naming the source that changed is the honest
version of "don't loosen the test to mask drift".

Rules for anything added here:
- The gate's default is off, so `pytest` on a laptop and in CI behaves the same.
- The job names the external dependency in its own name, so a red nightly says
  *which* upstream moved without anyone opening the log.
- A failure here is a real signal about the outside world, not a flake to
  re-run until green. Fix the code or the fixtures; do not relax the assertion.
- Never put a test here to escape the per-PR suite. Only genuine external
  dependencies qualify; anything that can run against a fixture stays on in CI.

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
Lives in `.github/` directly, NOT in `.github/workflows/`. GitHub reads it
natively — it is not a GitHub Actions workflow file.

**The file itself is authoritative.** What follows is the shape and the
reasoning; do not treat this block as a copy to edit. An earlier draft of this
section inlined the whole YAML, drifted from it completely, and described five
ecosystems that were never configured.

Five ecosystems are configured, all weekly on Mondays, all with a
`chore(deps)` commit prefix:

| Ecosystem | Directory | Covers |
|---|---|---|
| `uv` | `/` | Every Python service and package — one uv workspace at the root, so one entry covers all of them |
| `npm` | `/apps/web` | The React frontend |
| `npm` | `/apps/mobile` | The React Native app |
| `github-actions` | `/` | Pinned action versions in `workflows/` |
| `docker-compose` | `/` | The backing service images — local **and** CI, see below |

Terraform is commented out in the file, waiting on `infrastructure/terraform`
to actually contain `.tf` files.

Decisions worth knowing before changing it:
- **One `uv` entry, not one `pip` entry per directory.** uv is the package
  manager for this repo and the workspace root resolves every member together.
  Per-directory `pip` entries would open separate PRs for the same transitive
  bump in nine places and could resolve them inconsistently.
- **Minor and patch updates are grouped per ecosystem; majors come through
  individually.** A grouped PR keeps the volume low enough that people actually
  read them. A major arriving on its own gets a real review.
- **Majors of the core data and LLM stack are ignored entirely** — `pydantic`,
  `sqlalchemy`, `langchain*`. These are coordinated migrations, not bumps. Same
  for `expo` and `react-native` on mobile, and for `postgres` (see below).
- **Postgres majors are ignored, and this is not squeamishness.** Postgres 18
  moved its data directory into a major-version subdirectory, which breaks
  every existing local volume against the mount in `docker-compose.yml`. CI
  cannot catch that: it starts from an empty volume on every run, so the bump
  goes green in a pull request and breaks each developer only when they next
  pull. Upgrading means moving the mount to `/var/lib/postgresql` and planning
  a `pg_upgrade` or a deliberate reset — a task, not a merge.

### Backing service versions live in exactly one file
`docker-compose.yml` is the single source of truth for the postgres, redis and
qdrant versions. **CI does not declare its own service containers.** The test
job runs `docker compose up -d --wait postgres redis qdrant`, so a pull request
tests against the same images, the same healthchecks and the same configuration
a developer gets locally — not merely the same version tags.

This is deliberate and worth not undoing. Dependabot watches
`docker-compose.yml` and **cannot** watch images declared as GitHub Actions
service containers or job containers: that is
[dependabot-core#5819](https://github.com/dependabot/dependabot-core/issues/5819),
open since September 2022. While CI carried its own pin, nothing kept the two
in step and nothing could — a bump landed on the compose side alone and left
local dev on postgres 18 while every CI run stayed on 16, which is how a
migration passes on a laptop and fails in a pull request.

If a future job needs a backing service, add it to `docker-compose.yml` and
start it from there. Do not add a `services:` block back to `ci.yml`; that
reintroduces an unwatched second pin.

### .github/ISSUE_TEMPLATE/bug_report.md
See the file for the current template. The one rule that is not obvious from
reading it: **never paste PHI into an issue.** GitHub is not a HIPAA-eligible
store — no patient names, MRNs, dates of birth, addresses, real transcripts, or
real audio. Redact to synthetic values or reference a Synthea patient ID. The
template carries an "Impact" checkbox for possible PHI exposure; ticking it
means notifying the security owner directly rather than waiting on triage.

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
# Architecture Decision Records

Each file here records one decision: what was decided, what it was decided
against, and what accepting it costs. An ADR exists for a decision that would
otherwise be re-litigated — one where the obvious choice was not the one taken,
or where the reasoning lives outside the code that implements it.

## Format

Every record carries a **Status**, the **task** that made the decision, the
**Context** that forced it, the **Decision** itself, the **Consequences** of
accepting it, and **References** into the code and the tests that hold it in
place. Records are written in the past tense about the decision and the present
tense about the system.

## Rules

- **An accepted ADR is not edited to change its decision.** Reversing one means
  writing a new record that supersedes it, and marking the old one *Superseded
  by ADR-NNNN*. The history of what was believed and when is the point.
- Correcting a factual error, adding a reference, or clarifying prose is fine.
- **Numbers are never reused**, including for a record that gets withdrawn.
- A decision recorded here that is also a coding rule belongs in `CLAUDE.md` as
  well. `CLAUDE.md` states the rule; the ADR states why.

## Status values

| Status | Meaning |
|---|---|
| **Accepted** | In force, and implemented. |
| **Accepted (not yet implemented)** | Decided, but the code it governs does not exist yet. |
| **Superseded** | Replaced by a later record, which is named in the header. |

## Index

### Platform and language

| # | Decision | Status |
|---|---|---|
| [0001](./0001-claude-via-aws-bedrock.md) | Claude is called via AWS Bedrock, never the direct Anthropic API | Accepted |
| [0002](./0002-one-python-uv-workspace.md) | Every backend service is Python in one uv workspace | Accepted |
| [0003](./0003-redis-pubsub-not-kafka.md) | Redis pub/sub is the message bus until >20 providers | Accepted |
| [0004](./0004-self-hosted-qdrant.md) | Qdrant, self-hosted, is the vector store | Accepted |
| [0037](./0037-compose-is-the-only-version-pin.md) | `docker-compose.yml` is the only place backing-service versions are pinned | Accepted |
| [0038](./0038-gated-live-tests-need-a-schedule.md) | An env-gated live test is paired with a scheduled run | Accepted |

### PHI, audit and cryptography

| # | Decision | Status |
|---|---|---|
| [0005](./0005-audio-never-persists.md) | Encounter audio never touches disk | Accepted |
| [0006](./0006-audit-log-is-phi-only.md) | `audit_log` records PHI access and nothing else | Accepted |
| [0007](./0007-hipaa-logger-owns-its-table.md) | hipaa-logger owns its own table and writes it with raw asyncpg | Accepted |
| [0011](./0011-encryption-context-bound-twice.md) | Encryption context is bound twice — KMS and GCM AAD | Accepted |
| [0033](./0033-internal-callers-use-http.md) | Internal callers reach `/policies/query` over HTTP | Accepted |

### Schema and shared code

| # | Decision | Status |
|---|---|---|
| [0008](./0008-alembic-version-table-isolation.md) | Every Alembic setup gets its own version table | Accepted |
| [0009](./0009-one-sqlalchemy-model-definition.md) | The shared tables have one model definition, in track-a-clinical | Accepted |
| [0010](./0010-single-response-envelope-package.md) | One package defines the HTTP response envelope | Accepted |
| [0022](./0022-canonical-payer-slugs.md) | Payer identity is a canonical slug from one vocabulary package | Accepted |
| [0023](./0023-usps-jurisdictions-multi-state-policies.md) | Jurisdictions normalise to USPS codes; a multi-state policy is one document | Accepted |
| [0036](./0036-audio-wire-format-package.md) | The audio wire format is defined once, in a shared package | Accepted |

### Session identity and transport

| # | Decision | Status |
|---|---|---|
| [0012](./0012-single-session-jwt-issuer.md) | track-a-clinical is the only issuer of session JWTs | Accepted |
| [0013](./0013-two-websocket-token-carriers.md) | A WebSocket accepts the session token from either of two carriers | Accepted |
| [0028](./0028-per-session-subscription.md) | Consumers subscribe per session, announced on `sessions:started` | Accepted |

### Policy lookup — the core moat

| # | Decision | Status |
|---|---|---|
| [0014](./0014-two-stage-policy-answer.md) | The policy answer splits into a cacheable payer half and an uncached patient half | Accepted |
| [0015](./0015-fail-toward-authorization-required.md) | An unresolvable query fails toward "authorization required" | Accepted |
| [0016](./0016-two-tier-crd-and-rag.md) | Two-tier lookup: CRD decides `requires_auth`, RAG supplies the criteria | Accepted |
| [0017](./0017-crd-answers-are-never-cached.md) | A CRD answer is never cached | Accepted |
| [0018](./0018-crd-request-carries-no-patient.md) | The CRD request carries no patient | Accepted |
| [0032](./0032-deterministic-gap-analysis.md) | Gap analysis is deterministic Python, not a second model call | Accepted |

### Corpus ingestion and retrieval

| # | Decision | Status |
|---|---|---|
| [0019](./0019-qdrant-get-or-create.md) | Qdrant collections are get-or-create, never recreated | Accepted |
| [0020](./0020-qdrant-written-before-postgres.md) | Ingestion writes Qdrant before Postgres | Accepted |
| [0021](./0021-digest-over-uploaded-bytes.md) | The digest is over the uploaded bytes, and HTML is a first-class format | Accepted |
| [0024](./0024-scraper-reads-bulk-exports.md) | The scraper reads CMS bulk exports rather than crawling pages | Accepted |
| [0025](./0025-own-robots-txt-matcher.md) | robots.txt matching is implemented here, not taken from `urllib` | Accepted |

### Real-time detection

| # | Decision | Status |
|---|---|---|
| [0026](./0026-transcribe-medical-by-sdk-subclass.md) | Transcribe Medical is reached by subclassing the AWS streaming SDK | Accepted |
| [0027](./0027-publish-stabilized-segments-only.md) | Only stabilized transcript segments are published | Accepted |
| [0029](./0029-one-nudge-per-procedure-per-encounter.md) | One nudge per procedure per encounter, claimed atomically in Redis | Accepted |
| [0030](./0030-keyword-detection-not-a-model.md) | Procedure detection is a keyword list, not a model | Accepted |
| [0031](./0031-cpt-resolver-refuses-rather-than-guesses.md) | The CPT resolver refuses rather than guesses | Accepted |

### Clients

| # | Decision | Status |
|---|---|---|
| [0034](./0034-browser-capture-audioworklet.md) | Browser capture uses AudioWorklet, not MediaRecorder | Accepted |
| [0035](./0035-mobile-capture-expo-audio.md) | Mobile capture uses `expo-audio`'s `useAudioStream`, not `expo-av` | Accepted |
| [0039](./0039-typed-result-errors-in-clients.md) | Client errors are typed Result unions, never thrown | Accepted |

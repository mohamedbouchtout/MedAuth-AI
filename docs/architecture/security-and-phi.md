# Security, HIPAA and PHI Handling

This system processes Protected Health Information. HIPAA applies to every line
of code in it. This document collects the rules and, more usefully, says where
each one is *enforced* — because a rule that lives only in a document is a rule
that gets broken by the next person in a hurry.

## The seven standing rules

1. **Never log PHI** to stdout or any unencrypted store.
2. **Never write audio to disk** — process in memory, then discard.
3. **Every PHI access writes an `audit_log` row** via `hipaa-logger`.
4. **All secrets live in AWS Secrets Manager** — never in code, never in a
   committed `.env` file.
5. **TLS everywhere** — no plaintext HTTP internally or externally.
6. **Claude is reached through AWS Bedrock only** — the HIPAA-eligible path
   under the signed BAA ([ADR-0001](../adr/0001-claude-via-aws-bedrock.md)).
7. **Never paste PHI into a GitHub issue or PR.** GitHub is not a HIPAA-eligible
   store. No patient names, MRNs, dates of birth, addresses, real transcripts or
   real audio — redact to synthetic values or reference a Synthea patient ID.

## What counts as PHI here

| Data | PHI? | Where it lives |
|---|---|---|
| Encounter audio | **Yes** | Memory only, nowhere else |
| Transcript segments | **Yes** | Redis pub/sub, in flight |
| `clinical_context` on a policy query | **Yes** | Request body; reaches Stage 2 only |
| `patient_fhir_id`, `insurance_member_id` | **Yes** | `encounters` |
| SOAP note text | **Yes** | `clinical_notes` |
| Insurance policy documents | **No** | Public payer publications, no patient linkage |
| Payer slugs, CPT codes, state codes | **No** | Vocabulary and identifiers |
| `session_id`, `provider_id` | Identifiers | Logged freely; not clinical content |

The middle rows are why `POST /policies/ingest` writes **no** audit row while
`POST /policies/query` writes one.

## Where each rule is enforced

### "Never log PHI"

Not a review convention — a property of specific modules:

- **`packages/api-envelope`** — the validation handler reports field
  *locations* only. FastAPI's `RequestValidationError.errors()` can echo the
  rejected value, and request bodies here carry patient identifiers and clinical
  context. The constraint lives inside the primitive, not at call sites
  ([ADR-0010](../adr/0010-single-response-envelope-package.md)).
- **`packages/crypto-utils`** — no plaintext value or unwrapped key material
  reaches a log line, an exception message or a stack trace. A failure names the
  encryption context *keys* being processed and nothing else
  ([ADR-0011](../adr/0011-encryption-context-bound-twice.md)).
- **`audio-ingestion`** — audio bytes and transcript text pass through the
  WebSocket route and the publisher and are never logged. Log lines carry a
  session id, a byte count or a close reason.
- **`track-b-rag/keywords.py`** — the matched text is what was said in an exam
  room. Log lines about a detection name the canonical keyword ("MRI") and never
  the excerpt around it.
- **`track-b-rag/procedure_codes.py`** — the excerpt is matched against the
  qualifier vocabulary and never logged, and neither is the qualifier that
  matched: a body site is a clinical fact about a patient.
- **`track-b-rag/gap_analysis.py`** — the clinical context is read and
  referenced nowhere in the output. `missing_criteria` echoes the payer's own
  criteria text; `nudge_message` is built from those criteria and the procedure
  name.

### "Never write audio to disk"

Exactly one module on each tier holds audio at all — `audio.py` in the service,
`PcmFramer` in the shared package — so the claim is verifiable by reading two
files rather than trusted. Neither client uses a recording API capable of
producing a file: MediaRecorder and `expo-av` were both ruled out partly on this
ground ([ADR-0005](../adr/0005-audio-never-persists.md),
[ADR-0034](../adr/0034-browser-capture-audioworklet.md),
[ADR-0035](../adr/0035-mobile-capture-expo-audio.md)).

### "Every PHI access writes an audit row"

The rule is an **if and only if**: a route audits if and only if it touches PHI.
Both directions bind, because the table's value comes from every row in it being
a PHI access ([ADR-0006](../adr/0006-audit-log-is-phi-only.md)).

The audit call lives in the **route layer**, and internal callers reach a PHI
route over HTTP rather than importing its service function, so there is one call
path and therefore one audit site
([ADR-0033](../adr/0033-internal-callers-use-http.md)).

Where a query reads a PHI-bearing table but selects only non-PHI columns —
`policy_dispatch` reading `provider_id`, `insurance_payer`,
`insurance_plan_type` and `state` from `encounters` — the columns are named
explicitly in the SELECT. A later `select(Encounter)` would quietly turn that
into a PHI read, and naming the columns is what makes it not quiet in review.

### "All secrets in Secrets Manager"

No service reads a `.env` file. Values come from the process environment only:
local development exports them from `.env.local` (git-ignored), CI sets them on
the job, and deployments inject them from AWS Secrets Manager. Reading a file
from inside a service would add a fourth source of truth and a tempting place to
commit a secret.

`.env.example` ships every key with **no value**, so a shell that sources it
exports empty strings. Settings treat an empty variable as unset rather than as
a credential that happens to be the empty string.

### "TLS everywhere"

Deployment-level, not application-level, with one important qualification: the
CRD request built today carries no patient, so it is not a PHI disclosure and
plain HTTP to a local container is acceptable in development. **When TASK-059
adds a patient to that request, TLS stops being a deployment convention and
becomes a requirement**, the endpoint has to be verified per payer rather than
read from one `CRD_BASE_URL`, and the disclosure needs its own audit row
([ADR-0018](../adr/0018-crd-request-carries-no-patient.md)).

## Session credentials

- **One issuer.** `track-a-clinical`'s `POST /sessions/start` is the only minter
  of session JWTs. Claims are exactly `{session_id, provider_id, exp}`; HS256
  with `JWT_SIGNING_KEY`; lifetime from `SESSION_TTL_SECONDS`, default 900
  ([ADR-0012](../adr/0012-single-session-jwt-issuer.md)).
- **`session_id` is generated server-side**, never client-supplied.
- **A token is a credential** wherever it appears. Never logged, never in an
  error message, and never in a URL query string — the one place a credential is
  certain to be captured by intermediaries.
- **The server never echoes the token.** A WebSocket accept selects
  `medauth.session.v1` and never the `medauth.jwt.` entry, which would write the
  credential into the response headers and from there into every proxy access
  log on the path ([ADR-0013](../adr/0013-two-websocket-token-carriers.md)).
- **Rejection precedes the handshake.** An unauthenticated peer never reaches a
  state where it can send a frame.
- The 15-minute lifetime bounds the damage of a leak; TLS is what actually
  protects the handshake.

## Encryption

`packages/crypto-utils` provides field-level AES-256-GCM with a KMS-wrapped DEK
per record. This is for encrypting specific sensitive fields before they reach
the database — it is **not** a replacement for encryption at rest (RDS and S3
handle that separately) and it is **not** a general crypto toolkit.

The encryption context is bound **twice**: once as the KMS encryption context on
the DEK wrap and unwrap, and once as AES-GCM's AAD on the local operation. GCM
alone has no knowledge that its ciphertext was scoped to a record, so binding
only at the KMS layer would let ciphertext for one record's field be swapped onto
another and still decrypt. Binding the same context as AAD makes GCM's own
authentication tag reject the mismatch, independently of whether the KMS check
was ever bypassed ([ADR-0011](../adr/0011-encryption-context-bound-twice.md)).

## Third-party data flows

| Destination | Carries PHI? | Basis |
|---|---|---|
| AWS Transcribe Medical | **Yes** — encounter audio | HIPAA-eligible, BAA signed, `us-east-1` |
| AWS Bedrock (Claude) | **Yes** for SOAP; **no** for policy queries | Same. Policy prompts carry payer, plan, state, CPT and public policy text only, by construction ([ADR-0014](../adr/0014-two-stage-policy-answer.md)) |
| AWS Comprehend Medical | **Yes** *(TASK-031, planned)* | Same |
| AWS KMS | Key material only | Same |
| Qdrant | **No** — self-hosted anyway | [ADR-0004](../adr/0004-self-hosted-qdrant.md) |
| Embedding model | **No** — runs locally, nothing leaves | |
| Payer CRD endpoint | **No, today** | Patient-free request ([ADR-0018](../adr/0018-crd-request-carries-no-patient.md)) |
| CMS Medicare Coverage Database | **No** — outbound reads of public data | |
| EHR vendors *(Phase 5)* | **Yes** | SMART on FHIR, per-vendor agreements |

## Enforcement in the process

- **`.github/PULL_REQUEST_TEMPLATE.md`** carries a HIPAA checklist that has to be
  filled in honestly on every merge: no PHI in logs or error messages, new PHI
  access calls `audit_log()`, no secrets in code or comments, no audio written to
  disk, new environment variables added to `.env.example`.
- **`.github/CODEOWNERS`** puts `/packages/hipaa-logger/`,
  `/docs/compliance/` and production Terraform under explicit review.
- **CI** runs `bandit -r . -ll` on changed Python services alongside `ruff`,
  `mypy --strict` and an 80% coverage gate.
- **`.github/ISSUE_TEMPLATE/bug_report.md`** carries an Impact checkbox for
  possible PHI exposure. Ticking it means notifying the security owner directly
  rather than waiting on triage.

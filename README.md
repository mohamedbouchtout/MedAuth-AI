# MedAuth AI

Ambient clinical AI for healthcare providers. MedAuth listens to a physician–patient
encounter in real time, generates the SOAP note, and — the part nothing else on the
market does — queries insurance payer policies live during the visit, firing a nudge
the moment a procedure is ordered without the prior-authorization criteria the payer
requires. The physician finds out before the patient leaves the room, not three weeks
later in a denial letter.

> **This system processes PHI.** HIPAA applies to every line of code here. Read the
> regulatory rules in [CLAUDE.md](./CLAUDE.md) before contributing: no PHI in logs,
> no audio written to disk, every PHI access recorded through `hipaa-logger`, secrets
> in AWS Secrets Manager only, TLS everywhere.

---

## How it works

```
  microphone (web / mobile)
        │  250ms chunks, 16kHz mono, WebSocket
        ▼
  audio-ingestion ──► AWS Transcribe Medical ──► Redis  transcription:{session_id}
                                                   │
                        ┌──────────────────────────┴──────────────────────────┐
                        ▼                                                     ▼
              track-a-clinical                                        track-b-rag
              accumulates transcript,                    scans for procedure keywords,
              generates SOAP + ICD-10                    retrieves payer policy from
              via Claude Sonnet on Bedrock               Qdrant, reasons over it with
                        │                                Claude Sonnet on Bedrock
                        │                                                     │
                        │                                    Redis  nudges:{session_id}
                        │                                                     ▼
                        │                                            nudge-service
                        │                                     WebSocket relay to client
                        ▼                                                     ▼
              fhir-integration  ◄──── prior-auth ────►  live alert in the exam room
              SOAP note write-back    bundle assembly
              to the EHR              and submission
```

Two tracks run off one audio stream: Track A produces the documentation, Track B
produces the prior-auth intelligence. They share the transcript and nothing else.

## Repository layout

```
apps/web              React + TypeScript, SMART on FHIR launch
apps/mobile           React Native (Expo)
services/*            Seven Python services — see the diagram above
packages/*            hipaa-logger, api-envelope, crypto-utils, payer-vocab,
                      fhir-types, audio-wire (imported by services and apps)
infrastructure/       Terraform (AWS) and Kubernetes manifests
scripts/              Dev environment setup and seed data
docs/                 Architecture, design docs, ADRs, runbooks
docs/api/             OpenAPI spec per service
```

All backend services are Python in a single **uv workspace**. The npm workspaces
cover the two frontends only.

## Quick start

Requires Docker, [uv](https://docs.astral.sh/uv/), Node 24+, and the AWS CLI
configured with the `medauth-dev` profile.

```bash
cp .env.example .env.local     # fill in; never commit this file
docker compose up -d           # postgres, redis, qdrant, HAPI FHIR
uv sync --all-packages         # install every Python workspace member
npm install                    # install the frontends
```

Run a single service:

```bash
cd services/track-b-rag
uv run uvicorn src.main:app --reload --port 8002
```

### Local ports

| Port | Service | Port | Backing store |
|------|---------|------|---------------|
| 8080 | HAPI FHIR (synthetic EHR) | 5432 | PostgreSQL |
| 8001 | audio-ingestion | 6379 | Redis |
| 8002 | track-b-rag | 6333 | Qdrant |
| 8003 | track-a-clinical | — | — |
| 8004 | fhir-integration | — | — |
| 8005 | nudge-service | — | — |

## Stack

Python 3.12 · FastAPI · asyncio · PostgreSQL (asyncpg + SQLAlchemy 2.0 async) ·
Redis pub/sub · Qdrant · sentence-transformers (`BAAI/bge-large-en-v1.5`) ·
Claude via **AWS Bedrock** (Haiku for extraction, Sonnet for reasoning) ·
LangChain · React 19 + Vite + Tailwind · React Native (Expo SDK 57) ·
AWS us-east-1 on EKS, Terraform, GitHub Actions.

A few constraints are deliberate and non-negotiable: Bedrock rather than the direct
Anthropic API (that is the HIPAA-eligible path), Redis rather than Kafka until we
pass 20 providers, self-hosted Qdrant rather than a managed vector store, and audio
that lives only in memory. The reasoning behind each is in
[CLAUDE.md](./CLAUDE.md#key-architectural-constraints).

## EHR integrations

Everything goes through an adapter layer in `services/fhir-integration/src/adapters/`.
Standard FHIR R4 / US Core lives in `base.py`; only genuinely vendor-specific behavior
gets a subclass. The SMART launch `iss` parameter selects the adapter at runtime —
EHR type is never hardcoded.

Build order: **Athenahealth** first, then eClinicalWorks, Modernizing Medicine, Cerner,
and Epic last (largest market, hardest certification).

## Testing

```bash
uv run pytest                                  # from a service directory
uv run pytest --cov=src --cov-report=term-missing
```

pytest + pytest-asyncio, httpx for async clients, and **moto** for every AWS call —
tests never touch real Bedrock, Transcribe Medical, Comprehend Medical, or KMS.
CI fails below 80% coverage on services and packages.

## Contributing

Read [TASKS.md](./TASKS.md) before starting anything; it is the source of truth for
what is built, in progress, and next. Work is tracked by task number, and the number
belongs in the commit message:

```
feat(track-b-rag): implement policy query endpoint [TASK-012]
```

Pull requests run lint, type-check, and per-service tests, and carry a HIPAA
checklist that has to be filled in honestly. Open a task issue from the
`Task implementation` template to claim work.

## Status

Phases 0-2 are substantially complete: the shared packages, the database schema,
session lifecycle and JWT issuance, the full RAG pipeline including the Da Vinci
CRD tier, the audio WebSocket with Transcribe Medical, the transcript fan-out,
and both clients' capture layers.

Phases 3-10 — SOAP generation, the nudge relay, FHIR integration, prior auth
bundle assembly and the provider dashboard — are designed and unbuilt. The
current blocker on the live nudge path is **TASK-052b**: `encounters` carries
nullable payer, plan type and state columns that nothing populates until the
SMART on FHIR launch exists.

See [TASKS.md](./TASKS.md) for the authoritative task breakdown and
[docs/](./docs/README.md) for the architecture, the design documents and the
architecture decision records.

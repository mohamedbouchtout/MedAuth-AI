# Local Development

## Prerequisites

| Tool | Notes |
|---|---|
| Docker + Docker Compose | Brings up every backing service |
| [uv](https://docs.astral.sh/uv/) | The Python package manager. **Not pip, not poetry.** |
| Node 24+ and npm | Version lives in `.nvmrc`; CI reads that file |
| AWS CLI | Configured with the `medauth-dev` profile |

**Bedrock is the only AWS service called during local development**, and there is
no local mock for it. Everything else — Transcribe Medical, Comprehend Medical,
KMS — is mocked with moto in tests and not reached at all when running a service
by hand.

## First run

```bash
cp .env.example .env.local     # fill in; never commit this file
docker compose up -d           # postgres, redis, qdrant, HAPI FHIR, CRD RI
uv sync --all-packages         # install every Python workspace member
npm install                    # install the frontends
```

Then apply migrations, hipaa-logger first:

```bash
cd packages/hipaa-logger      && uv run alembic upgrade head
cd ../../services/track-a-clinical && uv run alembic upgrade head
```

Order matters: every service depends on `hipaa-logger`, so its `audit_log`
migration runs before any service-owned one
([ADR-0007](../adr/0007-hipaa-logger-owns-its-table.md)).

## Bringing it up

```powershell
./scripts/dev-up.ps1              # all six services + the web app
./scripts/dev-up.ps1 -Background  # same, logging to .dev-logs/ instead of windows
./scripts/dev-down.ps1            # stop them; leaves the compose stack up
```

The script exists for one reason, and it is the direct consequence of the
Configuration rule below: no service reads a `.env` file, so the values have to
be in the process environment before a service starts, and Windows has no
`source .env.local`. `dev-up.ps1` is that missing step. It also refuses to start
anything when `JWT_SIGNING_KEY`, `DATABASE_URL` or `SMART_WEB_RETURN_URL` is
empty, because the services that need those refuse to start anyway and a named
failure beats three tracebacks.

An empty variable in `.env.local` is left unexported rather than exported as an
empty string, matching how every `Settings` class already reads one — see
Configuration below.

## Backing services

`docker-compose.yml` is the single source of truth for the postgres, redis and
qdrant versions — CI starts these same containers rather than declaring its own
([ADR-0037](../adr/0037-compose-is-the-only-version-pin.md)).

| Service | Port | Image |
|---|---|---|
| PostgreSQL | 5432 | `postgres:16-alpine` |
| Redis | 6379 | `redis:8-alpine` |
| Qdrant | 6333 (REST), 6334 (gRPC) | `qdrant/qdrant:latest` |
| HAPI FHIR — synthetic EHR | 8080 | `hapiproject/hapi:latest` |
| Da Vinci CRD Reference Implementation | **8006** | `hlseven/davinci-crd:latest` |

The CRD container listens on **8090** internally and is published as **8006**
because Windows reserves the 8081–8180 range and the container cannot bind 8090
there.

To bring up only what a given service needs:

```bash
docker compose up -d --wait postgres redis qdrant
```

## Running a service

```bash
cd services/track-b-rag
uv run uvicorn track_b_rag.main:app --reload --port 8002
```

**The module path differs per service, and getting it wrong starts the wrong
service rather than failing.** Three services have renamed their package;
three still declare `packages = ["src"]` and therefore install one shared
top-level `src` into the workspace venv, where whichever sorts first wins
([ADR-0002](../adr/0002-one-python-uv-workspace.md)). `src.main:app` resolves
correctly for those three *only because* uvicorn is launched from that service's
own directory, which puts it ahead of the installed one. From anywhere else it
loads audio-ingestion.

| Port | Service | uvicorn target |
|---|---|---|
| 8001 | audio-ingestion | `src.main:app` |
| 8002 | track-b-rag | `track_b_rag.main:app` |
| 8003 | track-a-clinical | `track_a_clinical.main:app` |
| 8004 | fhir-integration | `src.main:app` |
| 8005 | nudge-service | `src.main:app` |
| 8007 | prior-auth | `prior_auth.main:app` |

Most settings have working local-dev defaults, but **four have none and the
services that need them refuse to start without them**, deliberately:

| Variable | Needed by | Why it has no default |
|---|---|---|
| `JWT_SIGNING_KEY` | audio-ingestion, track-a-clinical, nudge-service | A signing key with a default is a signing key everyone shares. Minimum 32 bytes. |
| `DATABASE_URL` | every service with a database | Guessing a database to write PHI into is worse than refusing. |
| `SMART_WEB_RETURN_URL` | fhir-integration | Validated at startup so a bad value surfaces there rather than at the end of an OAuth redirect chain, after a human has already logged in. |
| `SMART_MOBILE_RETURN_URI` | fhir-integration | The same, for the mobile return target. |

`dev-up.ps1` checks the first three before it starts anything.

The first `track-b-rag` request that embeds anything downloads
`BAAI/bge-large-en-v1.5` — roughly 1.3 GB — and takes seconds to load. It is a
lazy singleton, so a process that never embeds never pays for it.

## Configuration

No service reads a `.env` file. Values come from the process environment only:
`.env.local` for local dev (git-ignored), job-level variables in CI, and AWS
Secrets Manager in deployment. Reading a file from inside a service would add a
fourth source of truth and a tempting place to commit a secret.

`.env.example` ships every key with **no value**, so a shell that sources it
exports empty strings. Settings treat an empty variable as unset rather than as a
credential that happens to be the empty string.

`DATABASE_URL` is written in SQLAlchemy dialect form
(`postgresql+asyncpg://...`). hipaa-logger strips the `+asyncpg` suffix itself,
so one value works for every consumer.

## Seeding

```bash
uv run python scripts/seed-policies.py         # commercial payer policies -> Qdrant
uv run python -m policy_scraper                # Medicare LCDs -> Qdrant
uv run python scripts/seed-test-encounters.py  # encounter rows for manual testing
./scripts/seed-synthea.sh                      # 100 synthetic patients -> HAPI FHIR
```

`seed-policies.py` needs `track-b-rag` already running: it uploads documents and
the service does the work.

**Budget a long time for the first run — it is CPU-bound, not network-bound.**
Ingest chunks each document at 800 characters and embeds every chunk locally with
`BAAI/bge-large-en-v1.5`. The corpus is payer code lists, and they are enormous:
the BCBSMA Carelon extremity imaging PDF alone is 2.7 million characters, which
is **4,920 chunks** from one document. With no GPU that is minutes per document
and can be the better part of an hour for the corpus. It is a one-time cost —
ingest's `content_hash` dedup makes every later run a no-op for unchanged
documents — but the first run looks like a hang if you are not expecting it.
Watch it progress with:

```bash
curl -s localhost:6333/collections/insurance_policies | grep -o '"points_count":[0-9]*'
```

**After TASK-009, expect each Aetna document to report `updated` once more.**
Digests stored before that change were taken over the raw HTTP body, which
carried a CDN-injected script with a fresh token on every fetch; they are now
taken with script and style cut out
([ADR-0021](../adr/0021-digest-over-uploaded-bytes.md)). So the first seed run
after pulling it re-embeds the nine Aetna documents and reports them `updated`,
and every run after that reports `unchanged`. The BCBSMA PDFs and the CMS
documents are unaffected. An Aetna document reporting `updated` on two
consecutive runs is the bug, not this.

Seed and ingest under the **publishing licensee's** payer slug (`bcbs-ma`), never
a generic family bucket ([ADR-0022](../adr/0022-canonical-payer-slugs.md)).

## Driving an encounter without AWS

Transcribe Medical has no local mock, so with no AWS credentials there is no way
to get a transcript onto the bus from a microphone — and everything downstream of
the transcript is the product. `scripts/demo-encounter.py` closes that gap by
publishing a scripted encounter onto `transcription:{session_id}` itself, which
is the same channel and the same payload shape `audio-ingestion` publishes under:

```bash
uv run python scripts/demo-encounter.py                      # start a new session
uv run python scripts/demo-encounter.py --session-id <uuid>  # attach to a UI visit
uv run python scripts/demo-encounter.py --speed 0            # no pacing delay
```

It stands in for a producer rather than mocking a service, so everything it
exercises is real: keyword detection, CPT resolution, the dedup claim, the
policy dispatch, the `clinical_nudges` write and the WebSocket relay all run
exactly as they do from a microphone.

What it cannot substitute for is Bedrock. With no credentials the nudge is the
safe fallback — `requires_auth` true, `missing_criteria` empty, "confirm
manually" — and **seeding the corpus does not change that**, because retrieval
feeds Sonnet and it is Sonnet that produces the criteria.

**The CRD tier does not rescue it either, which is worth stating because it looks
like it should.** The Reference Implementation is local, needs no AWS and is
running. But CRD is specified as a patient-specific coverage check and our
request carries a placeholder subject by design
([ADR-0018](../adr/0018-crd-request-carries-no-patient.md)), so the RI answers
"unable to process" — no determination, and the RAG path answers alone. Verified
by running it. TASK-059 is what closes that, and it is gated on real `Patient`
and `Coverage` resources.

So a local demo without AWS proves the spine, not the intelligence. That is a
real and useful thing to be able to see; it is just not the whole product.

## Resetting the vector store

`qdrant.recreate_collection()` drops the collection and rebuilds it empty. It is
acceptable **only** in a one-off reset a human runs deliberately, and never in
application startup code, where it would silently wipe every indexed policy on
each restart ([ADR-0019](../adr/0019-qdrant-get-or-create.md)).

## Adding a migration

```bash
cd services/<name>
uv run alembic revision --autogenerate -m "description"
```

Rules:

- **Never alter a table by hand.**
- **Soft deletes** — add `deleted_at TIMESTAMPTZ`, never a hard `DELETE`.
- **Server-side UUIDs** via `gen_random_uuid()`, never client-generated.
- A new Alembic setup **must** declare
  `version_table="alembic_version_{name_with_underscores}"` in its `env.py`
  ([ADR-0008](../adr/0008-alembic-version-table-isolation.md)).

## Adding a route

1. Update `docs/api/<service-name>.yaml` — the OpenAPI spec is part of the change,
   not follow-up work. Each service has a contract test that fails when the spec
   and the app disagree.
2. Return the `api-envelope` shape. `GET /health` is the one documented departure
   ([ADR-0010](../adr/0010-single-response-envelope-package.md)).
3. Decide, explicitly, whether the route touches PHI. If it does, call
   `audit_log()`; if it does not, **do not**
   ([ADR-0006](../adr/0006-audit-log-is-phi-only.md)).
4. Add any new environment variable to `.env.example`.

## Adding a package

A new directory under `packages/` needs **its own CI path-filter entry and its
own test job**, with the same 80% coverage gate. A change under `packages/`
correctly re-runs every dependent service, but that is not a substitute for
running the package's own suite — that gap once left `hipaa-logger` never testing
itself. The CI wiring is part of the package's task, not scope creep.

## Common problems

**Migration state looks corrupt.** Two Alembic setups sharing `alembic_version`.
Check both `env.py` files declare a unique `version_table`.

**Retrieval returns nothing for a payer you know is indexed.** The payer string
did not normalise to the slug it was ingested under. The query path logs at
WARNING naming both spellings — that log line exists precisely to separate this
case from "we hold no policy for this payer".

**The audio socket connects and no transcript ever arrives.** A sample-rate
mismatch. Transcribe hangs rather than erroring, which is why both clients check
the rate twice before opening the socket. Check
`TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ` against `SAMPLE_RATE_HZ` in
`packages/audio-wire`.

**`import src.models` resolves to the wrong service.** Services declaring
`packages = ["src"]` all install a top-level module named `src` and shadow each
other in the shared venv. A service that grows importable code renames to
`src/<package>/` ([ADR-0002](../adr/0002-one-python-uv-workspace.md)).

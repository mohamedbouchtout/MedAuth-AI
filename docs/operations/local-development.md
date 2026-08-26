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

> `scripts/setup-dev.sh` and `scripts/seed-synthea.sh` are **stubs until
> TASK-052**. The commands above are what they will eventually wrap.

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
uv run uvicorn src.main:app --reload --port 8002
```

| Port | Service |
|---|---|
| 8001 | audio-ingestion |
| 8002 | track-b-rag |
| 8003 | track-a-clinical |
| 8004 | fhir-integration *(scaffold)* |
| 8005 | nudge-service *(scaffold)* |

Every service has working local-dev defaults for its settings, so it starts
against `docker compose up` with no environment set at all.

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
uv run python scripts/seed-test-encounters.py  # encounter rows for manual testing
./scripts/seed-synthea.sh                      # stub until TASK-052
```

Seed and ingest under the **publishing licensee's** payer slug (`bcbs-ma`), never
a generic family bucket ([ADR-0022](../adr/0022-canonical-payer-slugs.md)).

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

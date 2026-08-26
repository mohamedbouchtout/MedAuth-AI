# CI, Testing and Release

## Branches

- **`main` is protected.** No direct commits. Pull requests required, CI green
  before merge.
- **`feature/*`** — day-to-day work branches.
- **`release/*`** — triggers the production deploy, from Phase 6.

## Commit format

```
type(scope): description [TASK-XXX]
```

Types: `feat`, `fix`, `test`, `refactor`, `chore`, `docs`. Scope is a service or
package name.

Two rules that are enforced by review rather than tooling, and are easy to get
wrong:

- **The 50/72 rule.** Subject line at most 50 characters, body hard-wrapped at 72
  columns. The subject still has to carry the type, scope and task number, so it
  has to be genuinely terse and the explanation belongs in the body. The body is
  for the reasoning that is not visible in the diff.
- **Every commit must pass CI on its own**, not merely the tip of the branch.
  Order the work so tooling and configuration land before the code that depends
  on them, and squash or reorder any "fixes the previous commit" commit before
  opening a PR. History has to stay bisectable.

One logical change per commit. A branch mixing CI changes, docs and feature code
should be three commits, not one.

## `ci.yml`

Triggers on pull requests to `main` and pushes to `main`.

```
changes  (dorny/paths-filter)
   |
   +--> lint       ruff check + ruff format --check
   +--> typecheck  mypy src/                       (matrix over changed members)
   +--> test       pytest --cov=src --cov-fail-under=80
   +--> security   bandit -r . -ll
   +--> fhir-types pytest AND tsc --noEmit
   +--> audio-wire tsc --noEmit AND vitest
   +--> web        (apps/web)
   +--> mobile     (apps/mobile)
```

All jobs must pass before a PR can merge.

### Path filters

Each filter maps to a test job. A change under `packages/` re-runs every
dependent service *and* the package's own job.

| Filter | Matches |
|---|---|
| `hipaa-logger`, `api-envelope`, `crypto-utils`, `payer-vocab` | `packages/<name>/**` |
| `fhir-types` | `packages/fhir-types/**` — runs pytest **and** `tsc --noEmit` |
| `audio-wire` | `packages/audio-wire/**` — also sets `web` and `mobile` |
| Each service | `services/<name>/**` **or** `packages/**` |
| `web`, `mobile` | `apps/web/**`, `apps/mobile/**` |

> **Rule:** any directory under `packages/` needs its own path-filter entry
> **and** its own test job. A change under `packages/` re-running every dependent
> service is not a substitute for running the package's own suite — that gap once
> left `hipaa-logger` never testing itself.

`fhir-types` is the only package with two languages in one job. Its TypeScript
side is its own npm workspace, so `tsc` actually catches drift between the
Pydantic models and their TS mirrors rather than compiling them in isolation.

`audio-wire` ships source that both apps compile into themselves rather than a
built artefact, which is why a change there sets the app filters too.

### Backing services

The test job runs `docker compose up -d --wait postgres redis qdrant`. CI
declares **no** `services:` block of its own, so a PR tests against the same
images, healthchecks and configuration a developer gets locally
([ADR-0037](../adr/0037-compose-is-the-only-version-pin.md)).

Do not add a `services:` block back to `ci.yml` — Dependabot cannot watch images
declared that way, and that is how CI and local dev drifted onto different
Postgres majors last time.

The job also caches the HuggingFace model weights, because a 1.3 GB download per
run is not a reasonable per-PR cost.

## `nightly-live-checks.yml`

Some tests depend on a live external source. Those belong out of the per-PR
suite — an unrelated PR should not go red because a government site is down — so
they sit behind an environment-variable gate, defaulted off.

**A gate on its own is not a deferral, it is a deletion.** This workflow runs on
`schedule:` (05:00 UTC) with the gates set, plus `workflow_dispatch`
([ADR-0038](../adr/0038-gated-live-tests-need-a-schedule.md)).

| Job | Gate | External source |
|---|---|---|
| CMS Medicare Coverage Database | `RUN_CMS_LIVE_TESTS` | `downloads.cms.gov` |
| HL7 Da Vinci CRD Reference Implementation | `RUN_CRD_LIVE_TESTS` | The RI container |
| Aetna and BCBSMA policy documents | `RUN_PAYER_LIVE_TESTS` | Payer sites |

Four rules for anything added here:

- The gate defaults to off, so `pytest` on a laptop and in CI behave the same.
- **The job names the external dependency in its own name**, so a red nightly
  says *which* upstream moved without anyone opening the log.
- A failure is a real signal about the outside world, not a flake to re-run until
  green. Fix the code or the fixtures; **do not relax the assertion.**
- **Never put a test here to escape the per-PR suite.** Only genuine external
  dependencies qualify.

## Testing conventions

| | |
|---|---|
| Framework | pytest + pytest-asyncio; httpx for async clients |
| AWS mocking | **moto** for every AWS call — Bedrock, Transcribe Medical, Comprehend Medical, KMS |
| Layout | Tests mirror `src`: `src/services/rag.py` → `tests/unit/services/test_rag.py` |
| Coverage | 80% minimum on `services/` and `packages/`; CI fails below |
| Frontend | Vitest + React Testing Library (web); Jest + RNTL (mobile) |

**Unit tests** cover business logic — pure functions, no external calls.
**Integration tests** cover API routes against a test database with mocked AWS.

Two patterns worth copying:

- **Inject the seam, not the SDK.** The audio WebSocket route depends on a
  `TranscriptionStream` protocol, so the unit suite drives a full connection —
  handshake, frames, published segments, teardown — against a fake in
  milliseconds. track-b-rag does the same for Bedrock, Qdrant and the embedder.
- **Assert the wire, not the wrapper.** `test_transcribe_medical.py` asserts the
  *serialized request* — URI and both headers — rather than trusting a subclass
  to have taken effect, so an SDK release that restructures serialization fails
  the build instead of silently downgrading to the general model
  ([ADR-0026](../adr/0026-transcribe-medical-by-sdk-subclass.md)).

Anything that parses or maps an external source's output is validated against
**real output from that source**, with committed fixtures captured from real
responses backing the offline suite.

## Dependabot

Lives at `.github/dependabot.yml` — read natively by GitHub, **not** a workflow
file. Five ecosystems, all weekly on Mondays, all with a `chore(deps)` prefix:

| Ecosystem | Directory | Covers |
|---|---|---|
| `uv` | `/` | Every Python service and package — one workspace, one entry |
| `npm` | `/apps/web` | The React frontend |
| `npm` | `/apps/mobile` | The React Native app |
| `github-actions` | `/` | Pinned action versions |
| `docker-compose` | `/` | Backing service images, local **and** CI |

Terraform is commented out until `infrastructure/terraform` contains `.tf` files.

Decisions worth knowing before changing it:

- **One `uv` entry, not one `pip` entry per directory.** The workspace root
  resolves every member together; per-directory entries would open separate PRs
  for the same transitive bump in nine places and could resolve them
  inconsistently.
- **Minor and patch grouped per ecosystem; majors individually.** A grouped PR
  keeps the volume low enough that people actually read them; a major arriving
  alone gets a real review.
- **Majors of the core data and LLM stack are ignored entirely** — `pydantic`,
  `sqlalchemy`, `langchain*`, plus `expo` and `react-native`. Those are
  coordinated migrations, not bumps.
- **Postgres majors are ignored**, and not out of squeamishness: Postgres 18
  moved its data directory into a major-version subdirectory, breaking every
  existing local volume against the compose mount. CI starts from an empty volume
  every run, so the bump goes green in a PR and breaks each developer when they
  next pull. Upgrading is a task, not a merge.

## Pull requests

`.github/PULL_REQUEST_TEMPLATE.md` requires a task link, a description, test
evidence, and a HIPAA checklist filled in honestly:

- No PHI in logs, print statements, or error messages
- Any new PHI access calls `audit_log()`
- No secrets or credentials in code or comments
- No audio written to disk
- New environment variables added to `.env.example`

`.github/CODEOWNERS` puts `/packages/hipaa-logger/`, `/docs/compliance/` and
production Terraform under explicit review.

## Deploy

`.github/workflows/deploy-dev.yml` is a **stub** through Phases 0–5 —
`workflow_dispatch` only, echoing that the pipeline is not configured. It is
enabled in Phase 6 when the Terraform and Kubernetes manifests are real.

## Issues

`.github/ISSUE_TEMPLATE/task.md` for implementing a `TASKS.md` item;
`bug_report.md` for defects. The rule that is not obvious from reading the bug
template: **never paste PHI into an issue.** GitHub is not a HIPAA-eligible
store. Redact to synthetic values or reference a Synthea patient ID. The
template's Impact checkbox for possible PHI exposure means notifying the security
owner directly rather than waiting on triage.

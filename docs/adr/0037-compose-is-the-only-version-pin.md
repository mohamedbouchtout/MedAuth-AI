# ADR-0037: `docker-compose.yml` is the only place backing-service versions are pinned

**Status:** Accepted · **Task:** CI hardening

## Context

CI needs Postgres, Redis and Qdrant to run integration tests. The conventional
way is a `services:` block in the workflow, declaring each image and version.

That creates a second pin, and nothing keeps the two in step. **Dependabot
cannot watch images declared as GitHub Actions service containers or job
containers** — [dependabot-core#5819](https://github.com/dependabot/dependabot-core/issues/5819),
open since September 2022. While CI carried its own pin, a bump landed on the
compose side alone and left local dev on postgres 18 while every CI run stayed
on 16. That is how a migration passes on a laptop and fails in a pull request.

## Decision

`docker-compose.yml` is the **single source of truth** for the postgres, redis
and qdrant versions. CI declares no service containers of its own; the test job
runs:

```
docker compose up -d --wait postgres redis qdrant
```

A pull request therefore tests against the same images, the same healthchecks
and the same configuration a developer gets locally — not merely the same
version tags.

If a future job needs a backing service, it is added to `docker-compose.yml` and
started from there. **Do not add a `services:` block back to `ci.yml`.**

## Consequences

- Dependabot watches one file and its `docker-compose` ecosystem entry keeps
  both environments in step by construction.
- **Postgres majors are ignored by Dependabot, and this is not squeamishness.**
  Postgres 18 moved its data directory into a major-version subdirectory, which
  breaks every existing local volume against the mount in `docker-compose.yml`.
  CI cannot catch it: CI starts from an empty volume every run, so the bump goes
  green in a pull request and breaks each developer when they next pull.
  Upgrading means moving the mount to `/var/lib/postgresql` and planning a
  `pg_upgrade` or a deliberate reset — a task, not a merge.
- The same reasoning applies to the other ignored majors — `pydantic`,
  `sqlalchemy`, `langchain*`, `expo`, `react-native`. Those are coordinated
  migrations. Minor and patch updates are grouped per ecosystem so the volume
  stays low enough that people actually read the PRs; majors arrive individually
  and get a real review.
- This is an instance of a general preference: **remove the possibility of drift
  at its source rather than adding a CI check that detects it.**

## References

- `docker-compose.yml`, `.github/workflows/ci.yml`, `.github/dependabot.yml`
- `CLAUDE.md` -> Backing service versions live in exactly one file

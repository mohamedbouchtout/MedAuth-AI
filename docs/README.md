# MedAuth AI — Documentation

This directory is the written record of how MedAuth AI is built and why. It is
organised by the question you arrived with.

| If you want to know… | Read |
|---|---|
| What the system is and how the pieces fit together | [architecture/overview.md](./architecture/overview.md) |
| What each service does, what it depends on, and whether it exists yet | [architecture/service-catalog.md](./architecture/service-catalog.md) |
| What is stored, where, and under what key | [architecture/data-model.md](./architecture/data-model.md) |
| How PHI is handled, audited and protected | [architecture/security-and-phi.md](./architecture/security-and-phi.md) |
| How a nudge is actually produced | [design/rag-policy-lookup.md](./design/rag-policy-lookup.md) |
| How speech becomes a transcript | [design/audio-and-transcription.md](./design/audio-and-transcription.md) |
| Where session identity comes from | [design/session-lifecycle.md](./design/session-lifecycle.md) |
| How the policy corpus gets built | [design/policy-corpus-ingestion.md](./design/policy-corpus-ingestion.md) |
| **Why** a particular decision was made | [adr/](./adr/README.md) |
| How to run this locally | [operations/local-development.md](./operations/local-development.md) |
| How CI is wired and what it gates | [operations/ci-and-testing.md](./operations/ci-and-testing.md) |
| The HTTP contract of a service | [api/](./api/) — OpenAPI, one file per service |

## How these documents relate to the others in the repository

Four kinds of document coexist here and they are not interchangeable.

- **`CLAUDE.md`** is the standing instruction set: the rules that must be
  followed when writing code, stated as rules. It is normative and terse.
- **`TASKS.md`** is the work breakdown and the status of it: what is built, what
  is in progress, what is next, with acceptance criteria per task.
- **`docs/adr/`** records *decisions* — one per file, each with the alternatives
  that were rejected and what accepting it costs. An ADR is immutable once
  accepted; a decision that changes gets a new ADR that supersedes the old one.
- **`docs/architecture/` and `docs/design/`** are descriptive: they explain how
  the system currently works, and they are rewritten as it changes.

Where a rule appears in both `CLAUDE.md` and an ADR, `CLAUDE.md` states the rule
and the ADR states why it exists. Neither is a substitute for reading the code:
the module docstrings in this repository carry the reasoning at the point where
it applies, and they are the most reliable source of all.

## Scope note

These documents describe **implemented behaviour** except where they say
otherwise. Anything not yet built is marked *Planned* and names the task that
will build it. Phases 0–2 are substantially complete; Phases 3–10 are designed
but unbuilt. See [architecture/service-catalog.md](./architecture/service-catalog.md)
for the per-service breakdown and `TASKS.md` for the authoritative status.

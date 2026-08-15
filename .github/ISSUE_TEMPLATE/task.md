---
name: Task implementation
about: Track implementation of a numbered task from TASKS.md
title: "[TASK-XXX] <task name>"
labels: ["task"]
assignees: ""
---

## Task

- Task number: TASK-XXX
- Phase: <!-- e.g. Phase 1 — RAG Pipeline -->
- Service / package: <!-- e.g. services/track-b-rag -->

## Scope

<!-- Copy the bullets for this task from TASKS.md, so the issue is self-contained. -->

## Prerequisites

<!-- Tasks that must be complete first. TASKS.md states these for several tasks —
     e.g. TASK-056 requires TASK-055. Link the issues. -->

- [ ] TASK-XXX

## Acceptance criteria

<!-- Concrete and checkable. The **Test:** lines in TASKS.md are the starting point. -->

- [ ]
- [ ]

## Definition of done

- [ ] Implementation complete and matching the TASKS.md bullets
- [ ] Unit tests for business logic
- [ ] Integration tests for any new route, AWS mocked with moto
- [ ] Coverage at or above 80% on the touched service/package
- [ ] `hipaa-logger` called on every PHI access path
- [ ] `docs/api/<service-name>.yaml` updated if routes changed
- [ ] Alembic migration added if the schema changed
- [ ] `.env.example` updated if a new variable was introduced
- [ ] TASKS.md checkbox flipped to `[x]`
- [ ] Commit messages carry the task number, e.g. `feat(track-b-rag): ... [TASK-012]`

## Design notes and open questions

<!-- Decisions to make before starting, or that came up during implementation.
     Constraints worth re-reading first: Redis pub/sub not Kafka, Bedrock not the
     direct Anthropic API, Qdrant not Pinecone/Weaviate, Haiku for extraction and
     Sonnet for reasoning, audio never persists. -->

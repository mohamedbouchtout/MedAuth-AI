# [TASK-XXX] <short description>

<!--
Title format: feat(track-b-rag): implement policy query endpoint [TASK-012]
The TASK number is required — CI and review both key off it.
-->

## What this changes

<!-- One paragraph. What behavior is different after this merges? -->

## Task reference

- Implements: TASK-XXX
- TASKS.md updated to reflect new status: [ ] yes / [ ] not applicable

## How this was tested

<!-- Commands run, and what you saw. "CI is green" is not enough on its own. -->

```
```

---

## HIPAA checklist

Every box must be checked or explicitly marked N/A with a reason. These are not
formalities — each one maps to a rule in CLAUDE.md.

- [ ] No PHI is logged to stdout, stderr, or any unencrypted store
- [ ] No audio data is written to disk — in-memory buffers only, cleared on close
- [ ] Every code path that touches PHI calls `audit_log(...)` from `hipaa-logger`
- [ ] No secrets in code, in `.env` files, or in this diff — AWS Secrets Manager only
- [ ] `.env.example` updated if a new variable was introduced (names only, no values)
- [ ] All network calls use TLS — no plaintext HTTP, internal or external
- [ ] LLM calls go through AWS Bedrock (`langchain_aws.ChatBedrock`), not the direct
      Anthropic API, and not any other provider

## Code checklist

- [ ] Type hints on every function signature; no bare `Any` without justification
- [ ] Pydantic v2 syntax (`model_config = ConfigDict(...)`, `model_validate()`)
- [ ] SQLAlchemy 2.0 async style (`async with async_session() as session:`)
- [ ] No blocking calls inside `async def`
- [ ] No `print()` — `logging.getLogger(__name__)` instead
- [ ] Docstrings on all public functions

## For new or changed API routes

- [ ] Pydantic request and response models defined
- [ ] `hipaa-logger` call in the handler
- [ ] OpenAPI docstring written, and `docs/api/<service-name>.yaml` updated
- [ ] At least one integration test
- [ ] Response shape is `{"data": ..., "error": null}` / `{"data": null, "error": {...}}`
- [ ] Pagination is cursor-based (`?cursor=`), timestamps are ISO 8601 UTC

## For database changes

- [ ] Alembic migration generated — no manual `ALTER TABLE`
- [ ] Migration runs cleanly against a fresh local database
- [ ] Soft delete (`deleted_at TIMESTAMPTZ`), not a hard `DELETE`
- [ ] UUIDs generated server-side (`gen_random_uuid()`)

## Coverage

- [ ] Unit tests for new business logic
- [ ] Integration tests for new routes, with AWS mocked via moto (`@mock_aws`)
- [ ] Coverage on changed services/packages is at or above 80%

## Anything reviewers should look at closely

<!-- Trade-offs made, alternatives rejected, parts you are unsure about. -->

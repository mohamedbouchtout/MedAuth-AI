# ADR-0010: One package defines the HTTP response envelope

**Status:** Accepted · **Task:** TASK-010

## Context

Every endpoint in the system returns `{"data": ..., "error": null}` or
`{"data": null, "error": {...}}`. `track-a-clinical` implemented that first.
When `track-b-rag` became the second HTTP service, its envelope started life as
a copy.

Two hand-maintained definitions of a cross-service contract drift. The drift is
invisible until a client parses one shape from one service and a different one
from another.

## Decision

`packages/api-envelope` is the single definition, imported by every service. It
contains the envelope models, the machine-readable error codes,
`install_error_handlers()` for FastAPI's own failure paths, and
`error_responses()` for OpenAPI declarations.

Two constraints are locked into the package rather than left to call sites:

- **The validation handler never echoes a rejected value.** FastAPI's
  `RequestValidationError.errors()` reports the offending field *and can include
  what was sent*. Request bodies here carry patient identifiers and clinical
  context, so only field *locations* are ever reported. This is a HIPAA
  constraint living inside the primitive.
- **`error_responses()` carries generic per-status wording, overridable per
  route** via `descriptions={404: "..."}`. An undeclared status raises rather
  than publishing a spec with an invented description.

**Scope note:** this is not a shared web framework. No routes, no auth, no
middleware, no dependencies. A service's domain surface stays in that service.

## Consequences

- `GET /health` is the one documented departure from the failure half: a 503
  from a health endpoint returns `data` populated with per-dependency flags and
  `error: null`. The request succeeded; the answer is "unhealthy". Moving the
  flags into the error half would discard the endpoint's only diagnostic.
- A service added later imports this. Copying it again is the thing the package
  exists to prevent.
- The package gets its own CI job and its own 80% coverage gate, like every
  other member of `packages/`.

## References

- `packages/api-envelope/src/api_envelope/`
- `CLAUDE.md` -> packages/api-envelope — Design Decisions

# ADR-0002: Every backend service is Python in one uv workspace

**Status:** Accepted · **Task:** TASK-001

## Context

Early drafts of the architecture described `services/fhir-integration` as a
Node.js/Express/TypeScript service, on the reasoning that the SMART on FHIR
client libraries are strongest in JavaScript. That would have put one service on
a different runtime, test framework, linter, type checker, dependency manager
and deployment shape from the other six.

## Decision

All seven backend services are Python 3.12, managed as members of a single
**uv** workspace rooted at the repository's top-level `pyproject.toml`.
`fhir-integration` uses the `fhirclient` Python package.

npm workspaces exist at the root, but they cover only `apps/web`, `apps/mobile`
and the TypeScript packages those apps compile. No backend service is an npm
workspace member.

## Consequences

- One dependency resolution for the whole backend, so a transitive bump resolves
  consistently everywhere instead of nine times with nine answers. This is also
  why Dependabot has one `uv` entry at `/` rather than a `pip` entry per service.
- One toolchain: `ruff`, `mypy --strict`, `pytest` + `pytest-asyncio`, `httpx`.
  A developer moving between services learns nothing new.
- Cross-service imports become possible, which is what lets the shared
  SQLAlchemy models live in one place. See ADR-0009.
- The cost: the JavaScript SMART on FHIR ecosystem is unavailable to
  `fhir-integration`. `fhirclient` is less actively maintained than
  `fhir-kit-client`, and that is accepted.
- Services install as workspace members, so their top-level package names share
  one virtualenv. A service declaring `packages = ["src"]` installs a module
  literally named `src` and shadows every other service that does the same. That
  is tolerable only while nothing imports across the boundary; a service that
  grows importable code renames to `src/<package>/`.

## References

- Root `pyproject.toml`; `services/*/pyproject.toml`
- `CLAUDE.md` -> Tech Stack -> fhir-integration service

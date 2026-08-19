"""The response envelope every endpoint in this monorepo returns.

CLAUDE.md's API Design section fixes the shape: ``{"data": ..., "error": null}``
on success and ``{"data": null, "error": {...}}`` on failure. Every service
returns the same two shapes so a client parses one contract, not one per
service.

One documented exception exists so far: a service's ``GET /health`` answers 503
with ``data`` populated rather than ``error``. The request succeeded — the
answer is "unhealthy" — and moving the per-dependency flags into the error half
would discard the only diagnostic the endpoint has.
"""

from __future__ import annotations

from pydantic import BaseModel

#: Machine-readable code for a request that failed schema validation.
ERROR_CODE_VALIDATION = "validation_error"
#: Fallback code for an HTTP error raised without one.
ERROR_CODE_HTTP = "http_error"


class ApiError(BaseModel):
    """The error half of the envelope."""

    code: str
    message: str


class ApiResponse[DataT](BaseModel):
    """A response carrying either ``data`` or ``error`` — never both."""

    data: DataT | None = None
    error: ApiError | None = None

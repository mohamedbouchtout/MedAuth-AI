"""The response envelope every endpoint in this monorepo returns.

CLAUDE.md's API Design section fixes the shape: ``{"data": ..., "error": null}``
on success and ``{"data": null, "error": {...}}`` on failure. The handlers below
apply it to FastAPI's own failure paths too, so a validation rejection does not
escape in FastAPI's default ``{"detail": ...}`` form and force every client to
parse two shapes.

Deliberate near-duplicate of
``services/track-a-clinical/src/track_a_clinical/api/envelope.py`` — TASK-010
was scoped to reuse that *pattern*, not to introduce a shared HTTP package. Two
copies is the point at which this stops being free: the envelope is a
cross-service contract, and a third service copying it again should instead
move it into ``packages/`` and have all three import it. Flagged rather than
done here, because creating a shared package is a task of its own.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

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


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Return a JSON error envelope with the given status."""
    payload = ApiResponse[None](error=ApiError(code=code, message=message))
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that put FastAPI's own errors into the envelope."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = getattr(exc, "code", ERROR_CODE_HTTP)
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations only. This service's later routes carry a patient's
        # clinical context in the request body (TASK-012), and the exception's
        # own errors() output echoes offending values alongside their location.
        fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        return error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_CODE_VALIDATION,
            f"Request validation failed for: {fields}" if fields else "Request validation failed",
        )

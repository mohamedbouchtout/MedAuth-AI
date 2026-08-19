"""The response envelope every endpoint in this monorepo returns.

CLAUDE.md's API Design section fixes the shape: ``{"data": ..., "error": null}``
on success and ``{"data": null, "error": {...}}`` on failure. Handlers below
apply it to FastAPI's own failure paths too, so a 404 or a validation rejection
does not escape in FastAPI's default ``{"detail": ...}`` form and force every
client to parse two shapes.
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


def error_responses(*statuses: int) -> dict[int | str, dict[str, object]]:
    """Return OpenAPI ``responses`` entries declaring the error envelope.

    Without these, FastAPI documents its own ``HTTPValidationError`` shape for
    422 and omits the rest — a published spec describing a body no handler in
    this service ever returns.
    """
    described: dict[int, str] = {
        status.HTTP_404_NOT_FOUND: "The session is unknown or its encounter is soft-deleted.",
        status.HTTP_422_UNPROCESSABLE_CONTENT: "The request body or path parameter is invalid.",
        status.HTTP_503_SERVICE_UNAVAILABLE: (
            "The session ended but its signal could not be published."
        ),
    }
    return {code: {"description": described[code], "model": ApiResponse[None]} for code in statuses}


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Return a JSON error envelope with the given status."""
    payload = ApiResponse[None](error=ApiError(code=code, message=message))
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


class ApiHTTPException(StarletteHTTPException):
    """An HTTP error that carries a machine-readable code alongside its message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that put FastAPI's own errors into the envelope."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = getattr(exc, "code", ERROR_CODE_HTTP)
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # The exception's own errors() output names offending fields but can also
        # echo their values, and a request body here carries a patient identifier.
        # Only the field locations are reported.
        fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        return error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_CODE_VALIDATION,
            f"Request validation failed for: {fields}" if fields else "Request validation failed",
        )

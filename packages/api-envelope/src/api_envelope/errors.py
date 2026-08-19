"""Putting FastAPI's own failure paths into the envelope.

Without these handlers a 404 or a validation rejection escapes in FastAPI's
default ``{"detail": ...}`` form, so a client has to parse two shapes from the
same service. :func:`install_error_handlers` is called once per application, in
its ``create_app``.

The validation handler is the HIPAA-relevant one. ``RequestValidationError``'s
own ``errors()`` output names the offending field *and can echo its value*, and
request bodies in this monorepo carry patient identifiers and clinical context.
Only the field locations are ever reported.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api_envelope.models import ERROR_CODE_HTTP, ERROR_CODE_VALIDATION, ApiError, ApiResponse

#: Generic wording for the statuses services commonly declare. A route with
#: something more specific to say passes ``descriptions=`` to override it —
#: these are a floor, not a house style to work around.
DEFAULT_DESCRIPTIONS: Mapping[int, str] = {
    status.HTTP_400_BAD_REQUEST: "The request could not be processed as sent.",
    status.HTTP_401_UNAUTHORIZED: "Authentication is missing or invalid.",
    status.HTTP_403_FORBIDDEN: "The caller may not access this resource.",
    status.HTTP_404_NOT_FOUND: "The resource does not exist.",
    status.HTTP_409_CONFLICT: "The request conflicts with the current state.",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "The request body or path parameter is invalid.",
    status.HTTP_503_SERVICE_UNAVAILABLE: "A dependency this route needs is unavailable.",
}


class ApiHTTPException(StarletteHTTPException):
    """An HTTP error that carries a machine-readable code alongside its message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Return a JSON error envelope with the given status."""
    payload = ApiResponse[None](error=ApiError(code=code, message=message))
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def error_responses(
    *statuses: int,
    descriptions: Mapping[int, str] | None = None,
) -> dict[int | str, dict[str, object]]:
    """Return OpenAPI ``responses`` entries declaring the error envelope.

    Without these, FastAPI documents its own ``HTTPValidationError`` shape for
    422 and omits the rest — a published spec describing a body no handler in
    this monorepo ever returns.

    ``descriptions`` overrides the generic wording per status, for routes whose
    failure means something specific enough to be worth saying.
    """
    override = descriptions or {}
    described = {**DEFAULT_DESCRIPTIONS, **override}
    missing = [code for code in statuses if code not in described]
    if missing:
        raise KeyError(f"No description for status {missing}; pass descriptions= or add a default.")
    return {code: {"description": described[code], "model": ApiResponse[None]} for code in statuses}


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that put FastAPI's own errors into the envelope."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = getattr(exc, "code", ERROR_CODE_HTTP)
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations only — never the rejected value. See the module
        # docstring: these bodies carry patient identifiers and clinical text.
        fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        return error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_CODE_VALIDATION,
            f"Request validation failed for: {fields}" if fields else "Request validation failed",
        )

"""The shared HTTP response envelope every MedAuth service returns.

CLAUDE.md's API Design section fixes one response shape for the whole
monorepo — ``{"data": ..., "error": null}`` or ``{"data": null, "error": {...}}``
— and this package is the single definition of it. A service builds its app
like this::

    from api_envelope import ApiResponse, install_error_handlers

    def create_app() -> FastAPI:
        app = FastAPI(...)
        install_error_handlers(app)
        app.include_router(...)
        return app

    @router.get("/thing", response_model=ApiResponse[ThingData])
    async def thing() -> ApiResponse[ThingData]:
        return ApiResponse[ThingData](data=ThingData(...))

**Scope note:** this is the HTTP envelope and the handlers that apply it to
FastAPI's own failure paths. It is not a place for shared routes,
authentication, dependencies, or middleware — a service's domain surface stays
in that service.

It exists because ``track-a-clinical`` (TASK-006) and ``track-b-rag``
(TASK-010) both needed it and the second one started as a copy. Two
hand-maintained definitions of a cross-service contract drift, for the same
reason CLAUDE.md gives for centralising the shared SQLAlchemy models. Every
service added after this imports from here rather than copying again.
"""

from api_envelope.errors import (
    DEFAULT_DESCRIPTIONS,
    ApiHTTPException,
    error_response,
    error_responses,
    install_error_handlers,
)
from api_envelope.models import (
    ERROR_CODE_HTTP,
    ERROR_CODE_VALIDATION,
    ApiError,
    ApiResponse,
)

__all__ = [
    "DEFAULT_DESCRIPTIONS",
    "ERROR_CODE_HTTP",
    "ERROR_CODE_VALIDATION",
    "ApiError",
    "ApiHTTPException",
    "ApiResponse",
    "error_response",
    "error_responses",
    "install_error_handlers",
]

"""``GET /health`` — readiness of the one dependency this service cannot work without.

Redis is that dependency, and the flag is fatal rather than advisory: the launch
records live there, so a service that cannot reach it can neither start a launch
nor complete one. The EHR's own endpoints are deliberately not probed — there is
no single EHR to probe, and reachability of a vendor's authorization server is a
property of that launch rather than of this service's readiness.

Two conventions this endpoint follows rather than invents, both settled in
CLAUDE.md:

* **No hipaa-logger call.** The probe touches no PHI, and auditing a Kubernetes
  liveness check on its polling interval is exactly the dilution Known
  Constraints #6 exists to prevent.
* **A 503 still carries ``data``, not ``error``.** The request succeeded; the
  answer is "unhealthy". The flags are the only diagnostic the endpoint has.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from api_envelope import ApiResponse
from src.api.dependencies import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

ComponentStatus = Literal["ok", "error"]


class HealthData(BaseModel):
    """Per-dependency readiness flags."""

    redis: ComponentStatus = Field(
        description="Whether the Redis store holding SMART launch records answers.",
    )

    @property
    def all_ok(self) -> bool:
        """Return whether every dependency reported ``ok``."""
        return self.redis == "ok"


#: Declares the 503 half of the contract to OpenAPI. Without it FastAPI documents
#: only the 200 and the published spec understates the endpoint.
HEALTH_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": "At least one dependency is unreachable. The body names which.",
        "model": ApiResponse[HealthData],
    }
}


async def _redis_ok(redis: Redis) -> bool:
    """Return whether Redis answers a ping."""
    try:
        await redis.ping()
    except Exception:  # noqa: BLE001 — any failure to answer is the same answer here
        logger.warning("Redis did not answer the health probe")
        return False
    return True


@router.get(
    "/health",
    response_model=ApiResponse[HealthData],
    summary="Service health",
    response_description="Readiness of the Redis store SMART launch records live in.",
    responses=HEALTH_RESPONSES,
)
async def health(
    response: Response,
    redis: Annotated[Redis, Depends(get_redis)],
) -> ApiResponse[HealthData]:
    """Report whether this service can start and complete a SMART launch.

    Returns 200 when Redis answers and 503 when it does not. The body is the
    standard envelope in both cases.

    Deliberately unauthenticated and unaudited: this is what the Kubernetes
    readiness probe calls, and it exposes no patient data.
    """
    data = HealthData(redis="ok" if await _redis_ok(redis) else "error")
    if not data.all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ApiResponse[HealthData](data=data)

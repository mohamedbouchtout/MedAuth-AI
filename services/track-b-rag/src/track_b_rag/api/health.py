"""``GET /health`` — readiness for the two things this service cannot work without.

Reports one flag per dependency and answers 200 only when both are ``ok``; any
failure is 503. The body carries the per-dependency flags in both cases, so a
503 says *which* half is down rather than only that something is.

Two deliberate departures from the conventions, both settled rather than
improvised:

* **No hipaa-logger call.** A health check touches no PHI, and auditing a
  liveness probe on its polling interval is noise that contradicts that
  package's own scope note. CLAUDE.md records this as a standing exemption from
  Known Constraints #6 for every service's health endpoint, not a call made
  here.
* **A 503 still carries ``data``, not ``error``.** The request succeeded; the
  answer is "unhealthy". Moving the flags into the error half would throw away
  the only diagnostic the endpoint has, and a probe reads the status code
  anyway.

Both checks are synchronous and blocking — the Qdrant client is the sync one and
loading the embedding model is CPU-bound — so they run in a worker thread rather
than on the event loop, and concurrently with each other, because the first
probe after startup pays for a multi-second model load.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from starlette.concurrency import run_in_threadpool

from track_b_rag import embeddings, vector_store
from track_b_rag.api.dependencies import get_qdrant
from track_b_rag.api.envelope import ApiResponse

router = APIRouter(tags=["health"])

ComponentStatus = Literal["ok", "error"]


class HealthData(BaseModel):
    """Per-dependency readiness flags."""

    qdrant: ComponentStatus = Field(description="Whether the Qdrant vector store answers.")
    embedding_model: ComponentStatus = Field(
        description="Whether the local sentence-transformers model is loaded and usable.",
    )

    @property
    def all_ok(self) -> bool:
        """Return whether every dependency reported ``ok``."""
        return self.qdrant == "ok" and self.embedding_model == "ok"


#: Declares the 503 half of the contract to OpenAPI. Without it FastAPI
#: documents only the 200 and the published spec understates the endpoint.
HEALTH_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": "At least one dependency is unreachable. The body names which.",
        "model": ApiResponse[HealthData],
    }
}


def _flag(healthy: bool) -> ComponentStatus:
    """Map a boolean check result onto the wire vocabulary."""
    return "ok" if healthy else "error"


async def _probe_all(checks: dict[str, Callable[[], bool]]) -> dict[str, bool]:
    """Run every blocking check in a worker thread, all at once."""
    results: dict[str, bool] = {}

    async def run(name: str) -> None:
        results[name] = await run_in_threadpool(checks[name])

    async with anyio.create_task_group() as group:
        for name in checks:
            group.start_soon(run, name)

    return results


@router.get(
    "/health",
    response_model=ApiResponse[HealthData],
    summary="Service health",
    response_description="Readiness of the vector store and the embedding model.",
    responses=HEALTH_RESPONSES,
)
async def health(
    response: Response,
    client: Annotated[QdrantClient, Depends(get_qdrant)],
) -> ApiResponse[HealthData]:
    """Report whether Qdrant and the embedding model are both usable.

    Returns 200 when both are `ok` and 503 when either is `error`. The body is
    the standard envelope in both cases and always names the individual
    dependencies, so a failing probe is diagnosable from the response alone.

    Deliberately unauthenticated and unaudited: this is what the Kubernetes
    readiness probe calls, and it exposes no patient data.
    """
    results = await _probe_all(
        {
            "qdrant": lambda: vector_store.check_health(client),
            "embedding_model": embeddings.check_health,
        }
    )

    data = HealthData(
        qdrant=_flag(results["qdrant"]),
        embedding_model=_flag(results["embedding_model"]),
    )
    if not data.all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ApiResponse[HealthData](data=data)

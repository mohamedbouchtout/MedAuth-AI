"""``GET /health`` — readiness for the things this service cannot work without.

Reports one flag per dependency and answers 200 only when all of them are
``ok``; any failure is 503. The body carries the per-dependency flags in every
case, so a 503 says *which* one is down rather than only that something is.

Redis joined the list in TASK-012, when ``/policies/query`` started depending on
it. The dependency is real but not fatal: the query path degrades to computing
every answer from scratch when the cache is unreachable, which is correct but
pays Bedrock for work that should have cost nothing. An outage that only shows
up on the invoice is exactly the kind worth naming here.

The transcript consumer joined the list in TASK-021, and it is the one flag here
that is not a network dependency. It earns its place for the same reason the
others do, only more so: when that consumer is not running, no procedure keyword
is detected in any encounter on this pod and no nudge is raised for any of them,
and every visit looks exactly like a visit with nothing to flag. A silent
failure that is indistinguishable from success is precisely what a readiness
probe is for.

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

Two of the three checks are synchronous and blocking — the Qdrant client is the
sync one and loading the embedding model is CPU-bound — so they run in a worker
thread rather than on the event loop. The Redis client is natively async and
needs no thread. All three run concurrently, because the first probe after
startup pays for a multi-second model load.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from redis.asyncio import Redis
from starlette.concurrency import run_in_threadpool

from api_envelope import ApiResponse
from track_b_rag import cache, embeddings, vector_store
from track_b_rag.api.dependencies import get_qdrant, get_redis, get_transcript_consumer
from track_b_rag.transcript_consumer import TranscriptConsumer

router = APIRouter(tags=["health"])

ComponentStatus = Literal["ok", "error"]


class HealthData(BaseModel):
    """Per-dependency readiness flags."""

    qdrant: ComponentStatus = Field(description="Whether the Qdrant vector store answers.")
    embedding_model: ComponentStatus = Field(
        description="Whether the local sentence-transformers model is loaded and usable.",
    )
    redis: ComponentStatus = Field(
        description="Whether the Redis cache behind /policies/query answers.",
    )
    transcript_consumer: ComponentStatus = Field(
        description="Whether the transcript consumer is running and subscribed.",
    )

    @property
    def all_ok(self) -> bool:
        """Return whether every dependency reported ``ok``."""
        return all(
            flag == "ok"
            for flag in (
                self.qdrant,
                self.embedding_model,
                self.redis,
                self.transcript_consumer,
            )
        )


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


def _consumer_check(consumer: TranscriptConsumer | None) -> Callable[[], Awaitable[bool]]:
    """Adapt the consumer's liveness to the same shape as the other checks."""

    async def run() -> bool:
        return consumer is not None and consumer.is_healthy()

    return run


def _in_thread(check: Callable[[], bool]) -> Callable[[], Awaitable[bool]]:
    """Adapt a blocking check so it can be awaited alongside the async ones."""

    async def run() -> bool:
        return await run_in_threadpool(check)

    return run


async def _probe_all(checks: Mapping[str, Callable[[], Awaitable[bool]]]) -> dict[str, bool]:
    """Run every check at once, blocking ones already wrapped for a worker thread."""
    results: dict[str, bool] = {}

    async def run(name: str) -> None:
        results[name] = await checks[name]()

    async with anyio.create_task_group() as group:
        for name in checks:
            group.start_soon(run, name)

    return results


@router.get(
    "/health",
    response_model=ApiResponse[HealthData],
    summary="Service health",
    response_description="Readiness of the vector store, the embedding model and the cache.",
    responses=HEALTH_RESPONSES,
)
async def health(
    response: Response,
    client: Annotated[QdrantClient, Depends(get_qdrant)],
    redis: Annotated[Redis, Depends(get_redis)],
    consumer: Annotated[TranscriptConsumer | None, Depends(get_transcript_consumer)],
) -> ApiResponse[HealthData]:
    """Report whether Qdrant, the embedding model, Redis and the consumer are usable.

    Returns 200 when all four are `ok` and 503 when any is `error`. The body is
    the standard envelope in both cases and always names the individual
    dependencies, so a failing probe is diagnosable from the response alone.

    Deliberately unauthenticated and unaudited: this is what the Kubernetes
    readiness probe calls, and it exposes no patient data.
    """
    results = await _probe_all(
        {
            "qdrant": _in_thread(lambda: vector_store.check_health(client)),
            "embedding_model": _in_thread(embeddings.check_health),
            "redis": lambda: cache.check_health(redis),
            # No thread and no I/O: this asks a local object whether its task is
            # alive, which is the whole reason it can be probed this cheaply.
            "transcript_consumer": _consumer_check(consumer),
        }
    )

    data = HealthData(
        qdrant=_flag(results["qdrant"]),
        embedding_model=_flag(results["embedding_model"]),
        redis=_flag(results["redis"]),
        transcript_consumer=_flag(results["transcript_consumer"]),
    )
    if not data.all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ApiResponse[HealthData](data=data)

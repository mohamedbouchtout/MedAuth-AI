"""``GET /health`` — readiness for the things this service cannot work without.

Reports one flag per dependency and answers 200 only when all of them are
``ok``; any failure is 503. The body carries the flags in every case, so a 503
says *which* dependency is down rather than only that something is.

The endpoint arrives in TASK-030, later than it should have — this service ran
from TASK-006 with no readiness probe at all. What forced it is the transcript
consumer: when that consumer is not running, no encounter on this pod produces a
SOAP note, and nothing else in the system notices. TASK-060 would exhaust its
retries and log one warning per visit; the provider would see a visit that
generated nothing. A silent failure indistinguishable from success is precisely
what a readiness probe is for, and it is the same argument TASK-021 made when it
put ``transcript_consumer`` on track-b-rag's health endpoint.

**One consumer, one flag.** The consumer subscribes to three channel families —
``sessions:started``, each session's ``transcription:`` and each session's
``session:ended:`` — but they are served by a single task on a single
subscription, so a second "session end consumer" flag would report the same
fact twice. Two flags that cannot disagree are worse than one: they imply
coverage that does not exist. If the end handling is ever split onto its own
task, it earns its own flag then.

Two deliberate departures from the conventions, both inherited rather than
improvised — see track-b-rag's health module, which settled them:

* **No hipaa-logger call.** A health check touches no PHI, and auditing a
  liveness probe on its polling interval contradicts that package's scope note.
* **A 503 still carries ``data``, not ``error``.** The request succeeded; the
  answer is "unhealthy". Moving the flags into the error half would discard the
  only diagnostic the endpoint has.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from api_envelope import ApiResponse
from track_a_clinical.api.dependencies import get_redis, get_transcript_consumer
from track_a_clinical.consumer import TranscriptConsumer

router = APIRouter(tags=["health"])

ComponentStatus = Literal["ok", "error"]


class HealthData(BaseModel):
    """Per-dependency readiness flags."""

    redis: ComponentStatus = Field(
        description="Whether Redis answers — the session signals and the transcript bus.",
    )
    transcript_consumer: ComponentStatus = Field(
        description="Whether the transcript consumer is running and subscribed.",
    )

    @property
    def all_ok(self) -> bool:
        """Return whether every dependency reported ``ok``."""
        return all(flag == "ok" for flag in (self.redis, self.transcript_consumer))


#: Declares the 503 half of the contract to OpenAPI. Without it FastAPI
#: documents only the 200 and the published spec understates the endpoint.
HEALTH_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": "At least one dependency is unavailable. The body names which.",
        "model": ApiResponse[HealthData],
    }
}


def _flag(healthy: bool) -> ComponentStatus:
    """Map a boolean check result onto the wire vocabulary."""
    return "ok" if healthy else "error"


async def _redis_check(redis: Redis) -> bool:
    """Return whether Redis answers a ping."""
    try:
        await redis.ping()
    except (RedisError, OSError):
        return False
    return True


def _consumer_check(consumer: TranscriptConsumer | None) -> Callable[[], Awaitable[bool]]:
    """Adapt the consumer's liveness to the same shape as the other checks."""

    async def run() -> bool:
        return consumer is not None and consumer.is_healthy()

    return run


async def _probe_all(checks: dict[str, Callable[[], Awaitable[bool]]]) -> dict[str, bool]:
    """Run every check at once."""
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
    response_description="Readiness of Redis and the transcript consumer.",
    responses=HEALTH_RESPONSES,
)
async def health(
    response: Response,
    redis: Annotated[Redis, Depends(get_redis)],
    consumer: Annotated[TranscriptConsumer | None, Depends(get_transcript_consumer)],
) -> ApiResponse[HealthData]:
    """Report whether Redis and the transcript consumer are usable.

    Returns 200 when both are `ok` and 503 when either is `error`. The body is
    the standard envelope in both cases and always names the individual
    dependencies, so a failing probe is diagnosable from the response alone.

    Deliberately unauthenticated and unaudited: this is what the Kubernetes
    readiness probe calls, and it exposes no patient data.
    """
    results = await _probe_all(
        {
            "redis": lambda: _redis_check(redis),
            # No I/O: this asks a local object whether its task is alive, which
            # is the whole reason it can be probed this cheaply.
            "transcript_consumer": _consumer_check(consumer),
        }
    )

    data = HealthData(
        redis=_flag(results["redis"]),
        transcript_consumer=_flag(results["transcript_consumer"]),
    )
    if not data.all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ApiResponse[HealthData](data=data)

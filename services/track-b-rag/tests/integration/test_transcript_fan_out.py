"""TASK-021's claims against a real Redis.

What a fake cannot prove:

* **The channels line up end to end.** ``track-a-clinical`` announces a session
  on ``sessions:started`` and ``audio-ingestion`` publishes segments to
  ``transcription:{session_id}``; this consumer subscribes to both. Every one of
  those names is a string formatted in a different service, and a fake pub/sub
  agrees with whatever string it is handed. Only a real broker refuses to
  deliver when two services spell a channel differently.
* **Subscribing after an announcement actually receives.** The consumer
  subscribes to a channel from inside its own read loop, while that loop is
  mid-iteration on the same connection. That is the part most likely to behave
  differently against redis-py than against a hand-written double.
* **The dedup guard is atomic across real round trips**, rather than against a
  set in the test process.

The policy query itself is stubbed here — it has to be, because
:func:`resolve_query_parameters` cannot yet produce a payer, plan, state or CPT
code (TASK-024). What is asserted is that the dispatch is reached with the right
session and the right extracted context, which is the acceptance criterion
TASK-021 states.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from track_b_rag.api.schemas import PolicyQueryData
from track_b_rag.dedup import procedure_seen_key
from track_b_rag.policy_dispatch import PolicyQueryOutcome, PolicyQueryParameters
from track_b_rag.transcript_consumer import (
    SESSIONS_STARTED_CHANNEL,
    TranscriptConsumer,
    session_ended_channel,
    transcription_channel,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("REDIS_URL"),
        reason="needs a real Redis (REDIS_URL)",
    ),
]

#: Long enough for a publish to reach a subscriber on a local broker, short
#: enough that a genuine failure fails the suite quickly rather than hanging it.
SETTLE_SECONDS = 0.25


ENCOUNTER_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
PROVIDER_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")

PARAMETERS = PolicyQueryParameters(
    procedure="knee MRI",
    cpt_code="73721",
    payer="Aetna",
    plan_type="PPO",
    state="MA",
    provider_id=PROVIDER_ID,
    encounter_id=ENCOUNTER_ID,
)

ANSWER = PolicyQueryData(
    requires_auth=True,
    auth_criteria=["Failed six weeks of conservative therapy"],
    missing_criteria=["Failed six weeks of conservative therapy"],
    denial_risk="high",
    nudge_message="Prior authorization required for knee MRI.",
    step_therapy_required=False,
    step_therapy_details=None,
    policy_source="L33575",
    source="rag",
)


class RecordedDispatch:
    """Stands in for the policy query, which cannot run until TASK-052b."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> PolicyQueryOutcome:
        self.calls.append(kwargs)
        return PolicyQueryOutcome(parameters=PARAMETERS, answer=ANSWER)


class RecordedEmit:
    """Stands in for the nudge emitter, which needs a database this suite has not set up."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> uuid.UUID | None:
        self.calls.append(kwargs)
        return uuid.uuid4()


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(os.environ["REDIS_URL"])
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def dispatch() -> RecordedDispatch:
    return RecordedDispatch()


@pytest_asyncio.fixture
async def emit() -> RecordedEmit:
    return RecordedEmit()


@pytest_asyncio.fixture
async def consumer(
    redis: Redis, dispatch: RecordedDispatch, emit: RecordedEmit
) -> AsyncIterator[TranscriptConsumer]:
    running = TranscriptConsumer(redis, dispatch=dispatch, emit=emit)
    running.start()
    await _until(lambda: running.is_healthy())
    yield running
    await running.stop()


async def _until(predicate: Any, limit_seconds: float = 2.0) -> None:
    """Wait for `predicate`, rather than sleeping a guessed interval."""
    deadline = asyncio.get_running_loop().time() + limit_seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was never met")


async def announce(redis: Redis, session_id: uuid.UUID) -> None:
    """Publish what ``POST /sessions/start`` publishes."""
    await redis.publish(SESSIONS_STARTED_CHANNEL, json.dumps({"session_id": str(session_id)}))


async def publish_segment(redis: Redis, session_id: uuid.UUID, text: str) -> None:
    """Publish what ``audio-ingestion`` publishes for one stabilized segment."""
    await redis.publish(
        transcription_channel(session_id),
        json.dumps(
            {
                "session_id": str(session_id),
                "result_id": str(uuid.uuid4()),
                "text": text,
                "is_partial": False,
                "start_time": 4.0,
                "end_time": 7.5,
            }
        ),
    )


@pytest.fixture
def session_id() -> uuid.UUID:
    return uuid.uuid4()


async def test_an_mri_order_reaches_the_policy_query_with_its_context(
    redis: Redis,
    consumer: TranscriptConsumer,
    dispatch: RecordedDispatch,
    session_id: uuid.UUID,
) -> None:
    """TASK-021's acceptance test, over a real broker."""
    await announce(redis, session_id)
    await _until(lambda: session_id in consumer.watched_sessions)

    await publish_segment(
        redis,
        session_id,
        "The knee has been locking for months. Let's order an MRI.",
    )
    await _until(lambda: bool(dispatch.calls))

    (call,) = dispatch.calls
    assert call["session_id"] == session_id
    assert call["mention"].keyword == "MRI"
    assert call["clinical_context"] == {
        "transcript_excerpt": "The knee has been locking for months. Let's order an MRI."
    }

    await redis.delete(procedure_seen_key(session_id))


async def test_a_segment_published_before_the_announcement_is_not_received(
    redis: Redis,
    consumer: TranscriptConsumer,
    dispatch: RecordedDispatch,
    session_id: uuid.UUID,
) -> None:
    """Pub/sub has no backlog — which is why the announcement precedes the JWT.

    A client cannot open the audio socket before ``POST /sessions/start``
    returns, and that response is sent after the announcement is published, so
    in the real ordering this gap is closed. The test pins the property that
    makes the ordering matter rather than asserting a bug.
    """
    await publish_segment(redis, session_id, "Let's order an MRI.")
    await asyncio.sleep(SETTLE_SECONDS)

    assert dispatch.calls == []


async def test_a_repeat_mention_in_the_same_session_queries_once(
    redis: Redis,
    consumer: TranscriptConsumer,
    dispatch: RecordedDispatch,
    session_id: uuid.UUID,
) -> None:
    """The guard is a real SADD against a real broker, not a set in this process."""
    await announce(redis, session_id)
    await _until(lambda: session_id in consumer.watched_sessions)

    await publish_segment(redis, session_id, "Let's order an MRI.")
    await _until(lambda: bool(dispatch.calls))
    await publish_segment(redis, session_id, "So the MRI — I'll put that order in now.")
    await asyncio.sleep(SETTLE_SECONDS)

    assert len(dispatch.calls) == 1

    await redis.delete(procedure_seen_key(session_id))


async def test_ending_a_session_stops_delivery_and_clears_the_guard(
    redis: Redis,
    consumer: TranscriptConsumer,
    dispatch: RecordedDispatch,
    session_id: uuid.UUID,
) -> None:
    await announce(redis, session_id)
    await _until(lambda: session_id in consumer.watched_sessions)
    await publish_segment(redis, session_id, "Let's order an MRI.")
    await _until(lambda: bool(dispatch.calls))

    await redis.publish(session_ended_channel(session_id), "")
    await _until(lambda: session_id not in consumer.watched_sessions)

    await publish_segment(redis, session_id, "Also a biopsy of that lesion.")
    await asyncio.sleep(SETTLE_SECONDS)

    assert len(dispatch.calls) == 1
    assert await redis.exists(procedure_seen_key(session_id)) == 0


async def test_two_sessions_are_watched_independently(
    redis: Redis,
    consumer: TranscriptConsumer,
    dispatch: RecordedDispatch,
) -> None:
    """The same procedure in another encounter is another patient's nudge."""
    first, second = uuid.uuid4(), uuid.uuid4()
    await announce(redis, first)
    await announce(redis, second)
    await _until(lambda: {first, second} <= consumer.watched_sessions)

    await publish_segment(redis, first, "Let's order an MRI.")
    await publish_segment(redis, second, "Let's order an MRI.")
    await _until(lambda: len(dispatch.calls) == 2)

    assert {call["session_id"] for call in dispatch.calls} == {first, second}

    await redis.delete(procedure_seen_key(first), procedure_seen_key(second))

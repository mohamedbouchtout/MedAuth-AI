"""The transcript consumer: subscription lifecycle, detection, and dedup."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from tests.unit.test_dedup import FakeRedis as FakeRedisBase
from track_b_rag import transcript_consumer as consumer_module
from track_b_rag.api.schemas import PolicyQueryData
from track_b_rag.dedup import procedure_seen_key
from track_b_rag.policy_dispatch import (
    MissingQueryParameters,
    PolicyQueryOutcome,
    PolicyQueryParameters,
)
from track_b_rag.transcript_consumer import (
    SESSIONS_STARTED_CHANNEL,
    TranscriptConsumer,
    session_ended_channel,
    transcription_channel,
)

SESSION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

MRI_SEGMENT = {
    "session_id": str(SESSION_ID),
    "result_id": "result-1",
    "text": "Conservative therapy failed. Let's order an MRI.",
    "is_partial": False,
    "start_time": 12.5,
    "end_time": 16.25,
}


class FakePubSub:
    """Records subscriptions and hands back queued messages."""

    def __init__(self) -> None:
        self.channels: set[str] = set()
        self.messages: list[dict[str, Any]] = []
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        self.channels.update(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.channels.difference_update(channels)

    async def get_message(
        self,
        ignore_subscribe_messages: bool = False,
        timeout: float | None = None,  # noqa: ASYNC109 — mirrors redis-py's signature
    ) -> dict[str, Any] | None:
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(0)
        return None

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis(FakeRedisBase):
    """The dedup fake, plus a subscription."""

    def __init__(self, *, failing: bool = False) -> None:
        super().__init__(failing=failing)
        self.subscription = FakePubSub()

    def pubsub(self) -> FakePubSub:
        return self.subscription


ENCOUNTER_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
PROVIDER_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")

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
    """Stands in for the policy query seam."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raises = raises
        self.outcome = PolicyQueryOutcome(parameters=PARAMETERS, answer=ANSWER)

    async def __call__(self, **kwargs: Any) -> PolicyQueryOutcome:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.outcome


class RecordedEmit:
    """Stands in for the nudge emitter."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raises = raises

    async def __call__(self, **kwargs: Any) -> uuid.UUID | None:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return uuid.uuid4()


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def dispatch() -> RecordedDispatch:
    return RecordedDispatch()


@pytest.fixture
def emit() -> RecordedEmit:
    return RecordedEmit()


@pytest.fixture
def consumer(
    redis: FakeRedis, dispatch: RecordedDispatch, emit: RecordedEmit
) -> TranscriptConsumer:
    return TranscriptConsumer(redis, dispatch=dispatch, emit=emit)  # type: ignore[arg-type]


def announcement(session_id: uuid.UUID = SESSION_ID) -> dict[str, Any]:
    return {
        "channel": SESSIONS_STARTED_CHANNEL.encode(),
        "data": json.dumps({"session_id": str(session_id)}).encode(),
    }


def segment(**overrides: Any) -> dict[str, Any]:
    payload = {**MRI_SEGMENT, **overrides}
    return {
        "channel": transcription_channel(SESSION_ID).encode(),
        "data": json.dumps(payload).encode(),
    }


async def watch(consumer: TranscriptConsumer, pubsub: FakePubSub) -> None:
    """Put the consumer into the state it reaches after a session starts."""
    await consumer.handle_message(pubsub, announcement())


class TestSubscriptionLifecycle:
    """Per session, never a pattern subscribe over the PHI-carrying channels."""

    async def test_an_announcement_subscribes_to_that_session(
        self, consumer: TranscriptConsumer, redis: FakeRedis
    ) -> None:
        await watch(consumer, redis.subscription)

        assert redis.subscription.channels == {
            transcription_channel(SESSION_ID),
            session_ended_channel(SESSION_ID),
        }
        assert consumer.watched_sessions == frozenset({SESSION_ID})

    async def test_a_redelivered_announcement_does_not_subscribe_twice(
        self, consumer: TranscriptConsumer, redis: FakeRedis
    ) -> None:
        await watch(consumer, redis.subscription)
        await watch(consumer, redis.subscription)

        assert consumer.watched_sessions == frozenset({SESSION_ID})

    @pytest.mark.parametrize(
        "data",
        [b"not json", b"{}", b'{"session_id": "not-a-uuid"}', b"[]", b"null"],
    )
    async def test_an_unreadable_announcement_is_ignored(
        self,
        consumer: TranscriptConsumer,
        redis: FakeRedis,
        data: bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await consumer.handle_message(
            redis.subscription, {"channel": SESSIONS_STARTED_CHANNEL, "data": data}
        )

        assert consumer.watched_sessions == frozenset()
        assert "unreadable session-started announcement" in caplog.text

    async def test_the_end_signal_unsubscribes_and_clears_the_guard(
        self, consumer: TranscriptConsumer, redis: FakeRedis
    ) -> None:
        await watch(consumer, redis.subscription)
        await consumer.handle_message(redis.subscription, segment())

        await consumer.handle_message(
            redis.subscription,
            {"channel": session_ended_channel(SESSION_ID).encode(), "data": b""},
        )

        assert redis.subscription.channels == set()
        assert consumer.watched_sessions == frozenset()
        assert procedure_seen_key(SESSION_ID) not in redis.sets

    async def test_a_channel_with_an_unparseable_session_is_ignored(
        self, consumer: TranscriptConsumer, redis: FakeRedis, caplog: pytest.LogCaptureFixture
    ) -> None:
        await consumer.handle_message(
            redis.subscription, {"channel": "session:ended:nonsense", "data": b""}
        )

        assert "unparseable channel" in caplog.text

    async def test_a_message_on_an_unexpected_channel_is_ignored(
        self, consumer: TranscriptConsumer, redis: FakeRedis, caplog: pytest.LogCaptureFixture
    ) -> None:
        await consumer.handle_message(redis.subscription, {"channel": "nudges:x", "data": b"{}"})

        assert "ignored a message" in caplog.text


class TestSegmentHandling:
    """A keyword in a segment becomes exactly one policy query."""

    async def test_an_mri_mention_fires_the_query(
        self, consumer: TranscriptConsumer, redis: FakeRedis, dispatch: RecordedDispatch
    ) -> None:
        await watch(consumer, redis.subscription)

        await consumer.handle_message(redis.subscription, segment())

        (call,) = dispatch.calls
        assert call["session_id"] == SESSION_ID
        assert call["mention"].keyword == "MRI"

    async def test_the_query_carries_the_extracted_context(
        self, consumer: TranscriptConsumer, redis: FakeRedis, dispatch: RecordedDispatch
    ) -> None:
        """TASK-021's acceptance test: the surrounding sentences travel with it."""
        await watch(consumer, redis.subscription)

        await consumer.handle_message(redis.subscription, segment())

        (call,) = dispatch.calls
        assert call["clinical_context"] == {
            "transcript_excerpt": "Conservative therapy failed. Let's order an MRI."
        }

    async def test_the_context_carries_nothing_but_clinical_text(
        self, consumer: TranscriptConsumer, redis: FakeRedis, dispatch: RecordedDispatch
    ) -> None:
        """Stage 2 keeps digits of any length; a timestamp would pollute the terms."""
        await watch(consumer, redis.subscription)

        await consumer.handle_message(redis.subscription, segment())

        (call,) = dispatch.calls
        assert list(call["clinical_context"]) == ["transcript_excerpt"]
        assert "12.5" not in json.dumps(call["clinical_context"])
        assert "result-1" not in json.dumps(call["clinical_context"])

    async def test_a_partial_segment_is_skipped(
        self, consumer: TranscriptConsumer, redis: FakeRedis, dispatch: RecordedDispatch
    ) -> None:
        """audio-ingestion already drops these; this is the second line of defence."""
        await watch(consumer, redis.subscription)

        await consumer.handle_message(redis.subscription, segment(is_partial=True))

        assert dispatch.calls == []

    async def test_a_segment_with_no_procedure_fires_nothing(
        self, consumer: TranscriptConsumer, redis: FakeRedis, dispatch: RecordedDispatch
    ) -> None:
        await watch(consumer, redis.subscription)

        await consumer.handle_message(
            redis.subscription, segment(text="Her medications are unchanged.")
        )

        assert dispatch.calls == []

    @pytest.mark.parametrize("data", [b"not json", b"{}", b"[]"])
    async def test_an_unreadable_segment_is_ignored(
        self,
        consumer: TranscriptConsumer,
        redis: FakeRedis,
        data: bytes,
        dispatch: RecordedDispatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await consumer.handle_message(
            redis.subscription,
            {"channel": transcription_channel(SESSION_ID), "data": data},
        )

        assert dispatch.calls == []
        assert "unreadable transcript segment" in caplog.text

    async def test_two_procedures_in_one_segment_each_fire(
        self, consumer: TranscriptConsumer, redis: FakeRedis, dispatch: RecordedDispatch
    ) -> None:
        await watch(consumer, redis.subscription)

        await consumer.handle_message(
            redis.subscription, segment(text="Order an X-ray, and then an MRI.")
        )

        assert [call["mention"].keyword for call in dispatch.calls] == ["X-ray", "MRI"]

    async def test_no_segment_text_reaches_the_log(
        self, consumer: TranscriptConsumer, redis: FakeRedis, caplog: pytest.LogCaptureFixture
    ) -> None:
        """What was said in the room stays out of stdout."""
        with caplog.at_level("DEBUG"):
            await watch(consumer, redis.subscription)
            await consumer.handle_message(redis.subscription, segment())

        assert "order an MRI" not in caplog.text


class TestDedup:
    """One order, one nudge — however often it is said."""

    async def test_a_repeat_mention_in_a_later_segment_is_suppressed(
        self, consumer: TranscriptConsumer, redis: FakeRedis, dispatch: RecordedDispatch
    ) -> None:
        await watch(consumer, redis.subscription)

        await consumer.handle_message(redis.subscription, segment())
        await consumer.handle_message(
            redis.subscription, segment(text="So, the MRI — I'll get that ordered.")
        )

        assert len(dispatch.calls) == 1

    async def test_a_different_procedure_still_fires(
        self, consumer: TranscriptConsumer, redis: FakeRedis, dispatch: RecordedDispatch
    ) -> None:
        await watch(consumer, redis.subscription)

        await consumer.handle_message(redis.subscription, segment())
        await consumer.handle_message(
            redis.subscription, segment(text="Let's also do a biopsy of the lesion.")
        )

        assert [call["mention"].keyword for call in dispatch.calls] == ["MRI", "biopsy"]

    async def test_an_answered_query_reaches_the_emitter(
        self, redis: FakeRedis, dispatch: RecordedDispatch, emit: RecordedEmit
    ) -> None:
        """The wiring TASK-040 adds: the answer stops being thrown away.

        Everything before this task ended at the dispatch. What the emitter does
        with the answer is ``tests/unit/test_nudges.py``; what is asserted here
        is that it is handed the outcome's own parameters rather than anything
        this module reconstructed.
        """
        consumer = TranscriptConsumer(redis, dispatch=dispatch, emit=emit)  # type: ignore[arg-type]
        await watch(consumer, redis.subscription)

        await consumer.handle_message(
            redis.subscription, segment(text="Let's get an MRI of the knee.")
        )

        (call,) = emit.calls
        assert call["session_id"] == SESSION_ID
        assert call["parameters"] is PARAMETERS
        assert call["answer"] is ANSWER

    async def test_a_query_that_could_not_be_made_raises_no_nudge(
        self, redis: FakeRedis, dispatch: RecordedDispatch, emit: RecordedEmit
    ) -> None:
        """A failed query is not an answer, and silence is not "nothing to flag".

        ``post_policy_query`` logs at ERROR and returns None for a timeout or a
        transport failure. There is nothing to tell the provider — inventing a
        nudge from a query that never came back would put words in a payer's
        mouth — so the emitter is not called at all.
        """
        dispatch.outcome = PolicyQueryOutcome(parameters=PARAMETERS, answer=None)
        consumer = TranscriptConsumer(redis, dispatch=dispatch, emit=emit)  # type: ignore[arg-type]
        await watch(consumer, redis.subscription)

        await consumer.handle_message(
            redis.subscription, segment(text="Let's get an MRI of the knee.")
        )

        assert dispatch.calls
        assert emit.calls == []

    async def test_a_failing_emit_gives_the_claim_back(
        self, redis: FakeRedis, dispatch: RecordedDispatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A nudge that could not be raised must not silence the procedure.

        The emit sits inside the dedup claim, so a Redis or database failure
        there lands in the same handler a failed query does and the claim goes
        back. The next mention retries; the unique index from migration 0005 is
        what stops that retry becoming a second row.
        """
        failing = RecordedEmit(raises=RuntimeError("redis is unreachable"))
        consumer = TranscriptConsumer(redis, dispatch=dispatch, emit=failing)  # type: ignore[arg-type]
        await watch(consumer, redis.subscription)

        await consumer.handle_message(
            redis.subscription, segment(text="Let's get an MRI of the knee.")
        )
        await consumer.handle_message(
            redis.subscription, segment(text="Let's get an MRI of the knee.")
        )

        assert len(failing.calls) == 2
        assert "Policy query or nudge failed" in caplog.text

    async def test_a_transient_failure_gives_the_claim_back(
        self, redis: FakeRedis, emit: RecordedEmit, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Otherwise one bad moment silences that procedure for the whole visit."""
        failing = RecordedDispatch(raises=RuntimeError("connection reset"))
        consumer = TranscriptConsumer(redis, dispatch=failing, emit=emit)  # type: ignore[arg-type]
        await watch(consumer, redis.subscription)

        await consumer.handle_message(redis.subscription, segment())
        await consumer.handle_message(redis.subscription, segment())

        assert len(failing.calls) == 2
        assert "Policy query or nudge failed" in caplog.text

    async def test_a_structural_failure_keeps_the_claim(
        self, redis: FakeRedis, emit: RecordedEmit, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The remaining gap is logged once per procedure, not once per segment."""
        blocked = RecordedDispatch(raises=MissingQueryParameters(("state", "payer")))
        consumer = TranscriptConsumer(redis, dispatch=blocked, emit=emit)  # type: ignore[arg-type]
        await watch(consumer, redis.subscription)

        await consumer.handle_message(redis.subscription, segment())
        await consumer.handle_message(redis.subscription, segment())

        assert len(blocked.calls) == 1
        assert "state, payer" in caplog.text

    async def test_the_reason_reaches_the_log_when_there_is_one(
        self, redis: FakeRedis, emit: RecordedEmit, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing cpt_code has four causes and only one means "fix the table"."""
        blocked = RecordedDispatch(
            raises=MissingQueryParameters(("cpt_code",), reason="axis_not_spoken: view count")
        )
        consumer = TranscriptConsumer(redis, dispatch=blocked, emit=emit)  # type: ignore[arg-type]
        await watch(consumer, redis.subscription)

        await consumer.handle_message(redis.subscription, segment())

        assert "axis_not_spoken" in caplog.text

    async def test_the_claim_is_held_on_the_code_so_one_order_raises_one_nudge(
        self, redis: FakeRedis, dispatch: RecordedDispatch, emit: RecordedEmit
    ) -> None:
        """TASK-024 moves the guard from the keyword to the CPT code.

        A knee MRI and a hip MRI are both 73721 — one authorization question, so
        one nudge. Under TASK-021's keyword claim these were indistinguishable
        and the question could not even be asked; now they share a claim.
        """
        consumer = TranscriptConsumer(redis, dispatch=dispatch, emit=emit)  # type: ignore[arg-type]
        await watch(consumer, redis.subscription)

        await consumer.handle_message(
            redis.subscription, segment(text="Let's get an MRI of the knee.")
        )
        await consumer.handle_message(
            redis.subscription, segment(text="And an MRI of the hip as well.")
        )

        assert len(dispatch.calls) == 1
        assert redis.sets[procedure_seen_key(SESSION_ID)] == {"cpt:73721"}

    async def test_two_distinct_codes_each_get_their_own_claim(
        self, redis: FakeRedis, dispatch: RecordedDispatch, emit: RecordedEmit
    ) -> None:
        """Sharing a claim is a property of sharing a code, not of sharing a keyword."""
        consumer = TranscriptConsumer(redis, dispatch=dispatch, emit=emit)  # type: ignore[arg-type]
        await watch(consumer, redis.subscription)

        await consumer.handle_message(
            redis.subscription, segment(text="Let's get an MRI of the knee.")
        )
        await consumer.handle_message(redis.subscription, segment(text="Also an MRI of the brain."))

        assert len(dispatch.calls) == 2

    async def test_a_procedure_with_no_code_is_still_claimed_on_its_keyword(
        self, redis: FakeRedis, dispatch: RecordedDispatch, emit: RecordedEmit
    ) -> None:
        """Otherwise an unmappable procedure would warn on every segment naming it."""
        consumer = TranscriptConsumer(redis, dispatch=dispatch, emit=emit)  # type: ignore[arg-type]
        await watch(consumer, redis.subscription)

        await consumer.handle_message(
            redis.subscription, segment(text="We'll do a biopsy of the lesion.")
        )

        assert redis.sets[procedure_seen_key(SESSION_ID)] == {"keyword:biopsy"}


class TestRunLoop:
    """Starting, stopping, health, and losing the connection."""

    async def test_it_subscribes_to_the_announcement_channel_and_reports_healthy(
        self, consumer: TranscriptConsumer, redis: FakeRedis
    ) -> None:
        consumer.start()
        await asyncio.sleep(0.01)

        try:
            assert redis.subscription.channels == {SESSIONS_STARTED_CHANNEL}
            assert consumer.is_healthy() is True
        finally:
            await consumer.stop()

    async def test_a_stopped_consumer_is_not_healthy(self, consumer: TranscriptConsumer) -> None:
        """The flag /health reads: no consumer means no nudges for anyone."""
        assert consumer.is_healthy() is False

        consumer.start()
        await asyncio.sleep(0.01)
        await consumer.stop()

        assert consumer.is_healthy() is False

    async def test_starting_twice_runs_one_loop(self, consumer: TranscriptConsumer) -> None:
        consumer.start()
        first = consumer._task
        consumer.start()

        try:
            assert consumer._task is first
        finally:
            await consumer.stop()

    async def test_stopping_a_consumer_that_never_started_is_a_no_op(
        self, consumer: TranscriptConsumer
    ) -> None:
        await consumer.stop()

        assert consumer.is_healthy() is False

    async def test_it_closes_its_subscription_on_the_way_out(
        self, consumer: TranscriptConsumer, redis: FakeRedis
    ) -> None:
        consumer.start()
        await asyncio.sleep(0.01)
        await consumer.stop()

        assert redis.subscription.closed is True

    async def test_a_queued_message_is_handled(
        self, consumer: TranscriptConsumer, redis: FakeRedis, dispatch: RecordedDispatch
    ) -> None:
        redis.subscription.messages = [announcement(), segment()]

        consumer.start()
        await asyncio.sleep(0.05)
        await consumer.stop()

        assert [call["mention"].keyword for call in dispatch.calls] == ["MRI"]

    async def test_a_lost_connection_is_reported_and_retried(
        self,
        consumer: TranscriptConsumer,
        redis: FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The sessions in flight are gone — that has to be said out loud."""
        from redis.exceptions import RedisError

        monkeypatch.setattr(consumer_module, "_RECONNECT_DELAY_SECONDS", 0.0)
        attempts: list[int] = []
        original = redis.subscription.subscribe

        async def flaky(*channels: str) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise RedisError("connection lost")
            await original(*channels)

        monkeypatch.setattr(redis.subscription, "subscribe", flaky)

        consumer.start()
        await asyncio.sleep(0.05)
        await consumer.stop()

        assert len(attempts) >= 2
        assert "no longer watched and will raise no nudges" in caplog.text


async def test_a_transcript_channel_with_an_unparseable_session_is_ignored(
    consumer: TranscriptConsumer,
    redis: FakeRedis,
    dispatch: RecordedDispatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The session id comes from the channel name; a malformed one scans nothing."""
    await consumer.handle_message(
        redis.subscription, {"channel": "transcription:nonsense", "data": b"{}"}
    )

    assert dispatch.calls == []
    assert "unparseable channel" in caplog.text

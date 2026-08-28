"""The transcript consumer: what it accumulates, and what it does when a session ends.

Driven through :meth:`TranscriptConsumer.handle_message` with a fake
subscription, so the subscribe/unsubscribe calls and the buffer's lifetime can
both be observed without Redis. The read loop itself is exercised in the
integration suite against the Redis ``docker compose`` already starts.

The ordering assertions here are the point of the file: the buffer must outlive
the generation and be released only after the write, because releasing it first
turns every failure in between into silent, unrecoverable data loss.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from track_a_clinical import soap
from track_a_clinical.consumer import (
    SESSIONS_STARTED_CHANNEL,
    TranscriptBuffer,
    TranscriptConsumer,
    session_ended_channel,
    transcription_channel,
)
from track_a_clinical.models import (
    SOURCE_COMPREHEND_MEDICAL,
    SOURCE_LLM_EXTRACTION,
    CodeValidation,
    ExtractedCode,
)
from track_a_clinical.soap import GeneratedNote, SoapSections

SESSION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
OTHER_SESSION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

NOTE = GeneratedNote(
    sections=SoapSections(subjective="s", objective="o", assessment="a", plan="p"),
    icd10_codes=[],
    cpt_codes=[],
)


class FakePubSub:
    """Records what the consumer subscribed to and unsubscribed from."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.unsubscribed.extend(channels)


def started(session_id: uuid.UUID) -> dict[str, Any]:
    return {
        "channel": SESSIONS_STARTED_CHANNEL,
        "data": json.dumps({"session_id": str(session_id)}),
    }


def segment(session_id: uuid.UUID, text: str, *, is_partial: bool = False) -> dict[str, Any]:
    return {
        "channel": transcription_channel(session_id),
        "data": json.dumps(
            {
                "session_id": str(session_id),
                "result_id": "r1",
                "text": text,
                "is_partial": is_partial,
                "start_time": 0.0,
                "end_time": 1.0,
            }
        ),
    }


def ended(session_id: uuid.UUID) -> dict[str, Any]:
    return {"channel": session_ended_channel(session_id), "data": ""}


class Recorder:
    """A stand-in for soap.generate that records and can be made to fail or stall."""

    def __init__(self, note: GeneratedNote | None = NOTE) -> None:
        self.note = note
        self.transcripts: list[str] = []
        self.gate: asyncio.Event | None = None
        self.raises: Exception | None = None

    async def __call__(self, transcript: str, *, session_id: uuid.UUID) -> GeneratedNote | None:
        self.transcripts.append(transcript)
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        return self.note

    @property
    def calls(self) -> int:
        return len(self.transcripts)


class FakeSession:
    """Enough of an AsyncSession for the store path, which is stubbed anyway."""


def session_factory() -> Callable[[], Any]:
    """Return a factory yielding a fake database session."""

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        yield FakeSession()

    return factory


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Record what reached the note writer, without a database."""
    written: list[uuid.UUID] = []

    class FakeEncounter:
        id = uuid.uuid4()
        session_id = SESSION_ID
        provider_id = uuid.uuid4()

    async def load_encounter(session: Any, session_id: uuid.UUID) -> Any:
        return FakeEncounter()

    async def store_note(session: Any, *, encounter: Any, note: Any) -> uuid.UUID:
        written.append(encounter.id)
        return encounter.id

    from track_a_clinical import notes

    monkeypatch.setattr(notes, "load_encounter", load_encounter)
    monkeypatch.setattr(notes, "store_note", store_note)
    return written


async def _no_reconciliation(codes: list[ExtractedCode], transcript: str) -> list[ExtractedCode]:
    """Stand in for the Comprehend pass, which these tests are not about.

    Injected rather than left to the default, so no test in this module can
    reach AWS. TASK-031's own behaviour is covered in ``test_comprehend.py``;
    what is tested here is only that the consumer calls it in the right place.
    """
    return codes


def build(
    generate: Recorder,
    reconcile_icd10: Any = _no_reconciliation,
) -> tuple[TranscriptConsumer, FakePubSub]:
    """Return a consumer wired to a fake subscription and a stubbed generator."""
    consumer = TranscriptConsumer(
        redis=None,  # type: ignore[arg-type]
        session_factory=session_factory(),
        generate=generate,
        reconcile_icd10=reconcile_icd10,
    )
    return consumer, FakePubSub()


async def drain(consumer: TranscriptConsumer) -> None:
    """Wait for every generation task the consumer has in flight."""
    while consumer._generations:  # noqa: SLF001 — the queue is the thing under test
        await asyncio.gather(*consumer._generations.values())  # noqa: SLF001


# --- the buffer ------------------------------------------------------------


def test_a_buffer_joins_its_segments_in_order() -> None:
    buffer = TranscriptBuffer()
    buffer.add("first part")
    buffer.add("second part")

    assert buffer.transcript() == "first part second part"
    assert buffer.segment_count == 2


def test_a_buffer_ignores_an_empty_segment() -> None:
    buffer = TranscriptBuffer()
    buffer.add("   ")

    assert buffer.segment_count == 0
    assert buffer.transcript() == ""


# --- subscription ----------------------------------------------------------


async def test_a_started_session_subscribes_to_its_two_channels(stored: list[Any]) -> None:
    consumer, pubsub = build(Recorder())

    await consumer.handle_message(pubsub, started(SESSION_ID))

    assert pubsub.subscribed == [
        transcription_channel(SESSION_ID),
        session_ended_channel(SESSION_ID),
    ]
    assert consumer.watched_sessions == frozenset({SESSION_ID})


async def test_no_pattern_subscription_is_ever_made(stored: list[Any]) -> None:
    """transcription:* would hand this consumer every encounter's speech."""
    consumer, pubsub = build(Recorder())

    await consumer.handle_message(pubsub, started(SESSION_ID))

    assert not any("*" in channel for channel in pubsub.subscribed)


async def test_a_redelivered_announcement_does_not_reset_the_buffer(
    stored: list[Any],
) -> None:
    consumer, pubsub = build(Recorder())
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "already said"))

    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert consumer._generations == {}  # noqa: SLF001
    assert pubsub.subscribed.count(transcription_channel(SESSION_ID)) == 1


async def test_an_unreadable_announcement_is_ignored(stored: list[Any]) -> None:
    consumer, pubsub = build(Recorder())

    await consumer.handle_message(pubsub, {"channel": SESSIONS_STARTED_CHANNEL, "data": "{"})

    assert consumer.watched_sessions == frozenset()


async def test_a_message_on_an_unknown_channel_is_ignored(stored: list[Any]) -> None:
    consumer, pubsub = build(Recorder())

    await consumer.handle_message(pubsub, {"channel": "something:else", "data": ""})

    assert consumer.watched_sessions == frozenset()


# --- accumulation ----------------------------------------------------------


async def test_segments_accumulate_into_the_session_transcript(stored: list[Any]) -> None:
    generate = Recorder()
    consumer, pubsub = build(generate)

    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "the knee hurts"))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "order an MRI"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert generate.transcripts == ["the knee hurts order an MRI"]


async def test_partial_results_are_dropped(stored: list[Any]) -> None:
    """audio-ingestion already drops them; this is the belt to that braces."""
    generate = Recorder()
    consumer, pubsub = build(generate)

    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "the kn", is_partial=True))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "the knee hurts"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert generate.transcripts == ["the knee hurts"]


async def test_two_sessions_accumulate_separately(stored: list[Any]) -> None:
    generate = Recorder()
    consumer, pubsub = build(generate)

    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, started(OTHER_SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "first visit"))
    await consumer.handle_message(pubsub, segment(OTHER_SESSION_ID, "second visit"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert generate.transcripts == ["first visit"]


async def test_a_segment_for_an_unwatched_session_is_dropped(stored: list[Any]) -> None:
    """Inventing a buffer would generate a note from a fragment of a visit."""
    consumer, pubsub = build(Recorder())

    await consumer.handle_message(pubsub, segment(SESSION_ID, "orphan"))

    assert consumer.watched_sessions == frozenset()


async def test_an_unreadable_segment_is_ignored(stored: list[Any]) -> None:
    generate = Recorder()
    consumer, pubsub = build(generate)
    await consumer.handle_message(pubsub, started(SESSION_ID))

    await consumer.handle_message(
        pubsub, {"channel": transcription_channel(SESSION_ID), "data": "not json"}
    )
    await consumer.handle_message(pubsub, segment(SESSION_ID, "real segment"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert generate.transcripts == ["real segment"]


async def test_a_segment_on_an_unparseable_channel_is_ignored(stored: list[Any]) -> None:
    consumer, pubsub = build(Recorder())

    await consumer.handle_message(pubsub, {"channel": "transcription:not-a-uuid", "data": "{}"})

    assert consumer.watched_sessions == frozenset()


# --- ending: the ordering this task exists to get right ---------------------


async def test_ending_unsubscribes_before_generating(stored: list[Any]) -> None:
    """A late segment must not land mid-generation, and a redelivery must no-op."""
    generate = Recorder()
    generate.gate = asyncio.Event()
    consumer, pubsub = build(generate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "said something"))

    await consumer.handle_message(pubsub, ended(SESSION_ID))

    assert pubsub.unsubscribed == [
        transcription_channel(SESSION_ID),
        session_ended_channel(SESSION_ID),
    ]
    generate.gate.set()
    await drain(consumer)


async def test_the_buffer_outlives_the_generation(stored: list[Any]) -> None:
    """Held across both LLM calls and the write — released only after the row."""
    generate = Recorder()
    generate.gate = asyncio.Event()
    consumer, pubsub = build(generate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "said something"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))

    await asyncio.sleep(0)
    assert consumer.watched_sessions == frozenset({SESSION_ID})

    generate.gate.set()
    await drain(consumer)
    assert consumer.watched_sessions == frozenset()


async def test_the_buffer_is_released_only_after_a_successful_write(
    stored: list[uuid.UUID],
) -> None:
    generate = Recorder()
    consumer, pubsub = build(generate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "said something"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert len(stored) == 1
    assert consumer.watched_sessions == frozenset()


async def test_a_failed_generation_keeps_the_transcript(stored: list[Any]) -> None:
    """The alternative loses the encounter with nothing recording that it did."""
    generate = Recorder(note=None)
    consumer, pubsub = build(generate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "said something"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert stored == []
    assert consumer.watched_sessions == frozenset({SESSION_ID})


async def test_a_raising_generation_keeps_the_transcript(stored: list[Any]) -> None:
    generate = Recorder()
    generate.raises = RuntimeError("bedrock is unreachable")
    consumer, pubsub = build(generate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "said something"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert consumer.watched_sessions == frozenset({SESSION_ID})


async def test_a_failed_write_keeps_the_transcript(
    stored: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retryable precisely because the insert is idempotent."""
    from track_a_clinical import notes

    async def failing_store(session: Any, *, encounter: Any, note: Any) -> uuid.UUID:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(notes, "store_note", failing_store)

    consumer, pubsub = build(Recorder())
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "said something"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert consumer.watched_sessions == frozenset({SESSION_ID})


async def test_a_missing_encounter_is_logged_and_nothing_is_written(
    stored: list[Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from track_a_clinical import notes

    async def no_encounter(session: Any, session_id: uuid.UUID) -> Any:
        return None

    monkeypatch.setattr(notes, "load_encounter", no_encounter)

    consumer, pubsub = build(Recorder())
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "said something"))
    with caplog.at_level("ERROR", logger="track_a_clinical.consumer"):
        await consumer.handle_message(pubsub, ended(SESSION_ID))
        await drain(consumer)

    assert stored == []
    assert "No encounter" in caplog.text


async def test_a_second_end_signal_generates_nothing_more(stored: list[uuid.UUID]) -> None:
    """The unique constraint stops the second row; this stops the second Sonnet call."""
    generate = Recorder()
    consumer, pubsub = build(generate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "said something"))

    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert generate.calls == 1
    assert len(stored) == 1


async def test_a_redelivery_during_generation_generates_nothing_more(
    stored: list[uuid.UUID],
) -> None:
    """The case the unsubscribe cannot cover: a redelivery already in flight."""
    generate = Recorder()
    generate.gate = asyncio.Event()
    consumer, pubsub = build(generate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "said something"))

    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await asyncio.sleep(0)
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    generate.gate.set()
    await drain(consumer)

    assert generate.calls == 1
    assert len(stored) == 1


async def test_ending_a_session_never_watched_generates_nothing(
    stored: list[uuid.UUID],
) -> None:
    generate = Recorder()
    consumer, pubsub = build(generate)

    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert generate.calls == 0


async def test_an_end_on_an_unparseable_channel_is_ignored(stored: list[Any]) -> None:
    consumer, pubsub = build(Recorder())

    await consumer.handle_message(pubsub, {"channel": "session:ended:nope", "data": ""})

    assert pubsub.unsubscribed == []


# --- liveness and shutdown -------------------------------------------------


async def test_a_consumer_that_was_never_started_is_not_healthy() -> None:
    consumer, _ = build(Recorder())

    assert consumer.is_healthy() is False


async def test_stopping_without_starting_is_a_no_op() -> None:
    consumer, _ = build(Recorder())

    await consumer.stop()

    assert consumer.is_healthy() is False


async def test_stopping_cancels_a_generation_in_flight(
    stored: list[uuid.UUID], caplog: pytest.LogCaptureFixture
) -> None:
    """Its transcript is in this process and goes with it — said, not hidden."""
    generate = Recorder()
    generate.gate = asyncio.Event()
    consumer, pubsub = build(generate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "said something"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await asyncio.sleep(0)

    with caplog.at_level("WARNING", logger="track_a_clinical.consumer"):
        await consumer.stop()

    assert stored == []
    assert "in flight" in caplog.text


async def test_a_dropped_redis_connection_reports_what_was_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A restart or a drop loses the visits in flight; the count is logged."""
    consumer, pubsub = build(Recorder())
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, started(OTHER_SESSION_ID))

    calls = 0

    async def failing_consume() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RedisConnectionError("gone")
        await asyncio.sleep(3600)

    consumer._consume = failing_consume  # type: ignore[method-assign]  # noqa: SLF001

    with caplog.at_level("WARNING", logger="track_a_clinical.consumer"):
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert "2 session(s)" in caplog.text
    assert consumer.watched_sessions == frozenset()


async def test_starting_twice_launches_one_read_loop() -> None:
    """The lifespan starts it once, but the call is idempotent by contract."""
    consumer, _ = build(Recorder())

    async def idle() -> None:
        await asyncio.sleep(3600)

    consumer.run = idle  # type: ignore[method-assign]
    consumer.start()
    first = consumer._task  # noqa: SLF001
    consumer.start()

    assert consumer._task is first  # noqa: SLF001
    await consumer.stop()


async def test_the_default_session_factory_opens_a_real_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the injected factory above against the real one being wrong."""
    opened: list[str] = []

    class FakeSessionMaker:
        def __call__(self) -> Any:
            opened.append("session")
            return self

        async def __aenter__(self) -> Any:
            return FakeSession()

        async def __aexit__(self, *exc: object) -> None:
            opened.append("closed")

    from track_a_clinical import consumer as consumer_module

    monkeypatch.setattr(consumer_module, "get_sessionmaker", lambda: FakeSessionMaker())

    async with consumer_module._default_session_factory() as session:  # noqa: SLF001
        assert isinstance(session, FakeSession)

    assert opened == ["session", "closed"]


def test_the_generation_seam_defaults_to_the_real_one() -> None:
    """Guards the stubbing above against a renamed or rerouted generator."""
    consumer = TranscriptConsumer(redis=None)  # type: ignore[arg-type]

    assert consumer._generate is soap.generate  # noqa: SLF001


# --- ICD-10 validation (TASK-031) ------------------------------------------


@pytest.mark.asyncio
async def test_codes_are_validated_before_the_note_is_stored(
    stored: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is written already validated, in one write.

    ``store_note`` is ``ON CONFLICT DO NOTHING`` and has no idempotent update
    path, so a validation pass running after the insert would have nothing to
    update on the retry of a duplicated signal.
    """
    seen: list[list[ExtractedCode]] = []
    written: list[Any] = []

    async def validate(codes: list[ExtractedCode], transcript: str) -> list[ExtractedCode]:
        seen.append(codes)
        return [
            code.model_copy(update={"validation": CodeValidation(confidence=0.9, confirmed=True)})
            for code in codes
        ]

    async def store_note(session: Any, *, encounter: Any, note: Any) -> uuid.UUID:
        written.append(note)
        return encounter.id

    from track_a_clinical import notes

    monkeypatch.setattr(notes, "store_note", store_note)

    note = GeneratedNote(
        sections=SoapSections(subjective="s", objective="o", assessment="a", plan="p"),
        icd10_codes=[ExtractedCode.from_llm("M17.11")],
        cpt_codes=[ExtractedCode.from_llm("73721")],
    )
    consumer, pubsub = build(Recorder(note), validate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "knee pain"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert seen == [[ExtractedCode.from_llm("M17.11")]]
    assert written[0].icd10_codes[0].validation is not None
    assert written[0].icd10_codes[0].validation.confirmed is True


@pytest.mark.asyncio
async def test_the_validator_receives_the_transcript_not_the_note(
    stored: list[Any],
) -> None:
    """Checking the LLM's codes against the LLM's own prose confirms nothing."""
    seen: list[str] = []

    async def validate(codes: list[ExtractedCode], transcript: str) -> list[ExtractedCode]:
        seen.append(transcript)
        return codes

    note = GeneratedNote(
        sections=SoapSections(
            subjective="written by the model", objective="o", assessment="a", plan="p"
        ),
        icd10_codes=[ExtractedCode.from_llm("M17.11")],
        cpt_codes=[],
    )
    consumer, pubsub = build(Recorder(note), validate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "spoken in the room"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert seen == ["spoken in the room"]


@pytest.mark.asyncio
async def test_a_validation_failure_still_stores_the_note(
    stored: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation is metadata; the note is the artifact the provider waits for."""
    written: list[Any] = []

    async def validate(codes: list[ExtractedCode], transcript: str) -> list[ExtractedCode]:
        raise RuntimeError("comprehend is down")

    async def store_note(session: Any, *, encounter: Any, note: Any) -> uuid.UUID:
        written.append(note)
        return encounter.id

    from track_a_clinical import notes

    monkeypatch.setattr(notes, "store_note", store_note)

    note = GeneratedNote(
        sections=SoapSections(subjective="s", objective="o", assessment="a", plan="p"),
        icd10_codes=[ExtractedCode.from_llm("M17.11")],
        cpt_codes=[],
    )
    consumer, pubsub = build(Recorder(note), validate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "knee pain"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert len(written) == 1
    assert written[0].icd10_codes[0].validation is None
    assert written[0].icd10_codes[0].source == SOURCE_LLM_EXTRACTION


@pytest.mark.asyncio
async def test_cpt_codes_are_never_sent_for_validation(stored: list[Any]) -> None:
    """Comprehend Medical has no CPT inference; those entries stay unvalidated."""
    seen: list[list[ExtractedCode]] = []

    async def validate(codes: list[ExtractedCode], transcript: str) -> list[ExtractedCode]:
        seen.append(codes)
        return codes

    note = GeneratedNote(
        sections=SoapSections(subjective="s", objective="o", assessment="a", plan="p"),
        icd10_codes=[ExtractedCode.from_llm("M17.11")],
        cpt_codes=[ExtractedCode.from_llm("73721")],
    )
    consumer, pubsub = build(Recorder(note), validate)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert seen == [[ExtractedCode.from_llm("M17.11")]]
    assert note.cpt_codes is not None
    assert note.cpt_codes[0].validation is None


@pytest.mark.asyncio
async def test_a_note_with_no_codes_is_still_reconciled(
    stored: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extraction pass finding nothing is where discovery is worth the most.

    So there is deliberately no short circuit on an empty code list: skipping
    the call to save a round trip would drop the finding in the one case it
    matters most. The reconciled codes reach the stored row, not just the
    in-memory note.
    """
    written: list[Any] = []
    called: list[str] = []

    async def reconcile(codes: list[ExtractedCode], transcript: str) -> list[ExtractedCode]:
        called.append(transcript)
        return [*codes, ExtractedCode.from_comprehend("I10", "Essential hypertension", 0.97)]

    async def store_note(session: Any, *, encounter: Any, note: Any) -> uuid.UUID:
        written.append(note)
        return encounter.id

    from track_a_clinical import notes

    monkeypatch.setattr(notes, "store_note", store_note)

    consumer, pubsub = build(Recorder(NOTE), reconcile)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "blood pressure is stable"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert called == ["blood pressure is stable"]
    assert [code.code for code in written[0].icd10_codes] == ["I10"]
    assert written[0].icd10_codes[0].source == SOURCE_COMPREHEND_MEDICAL
    assert written[0].icd10_codes[0].confidence == pytest.approx(0.97)


@pytest.mark.asyncio
async def test_a_failed_extraction_is_left_undetermined(
    stored: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` means the pass never answered, and suggestions must not fill it in.

    ``[]`` and ``None`` are different facts on this column — ran and found
    nothing, against never determined. Writing Comprehend's suggestions into
    the second would present a partial answer as the answer.
    """
    written: list[Any] = []
    called: list[str] = []

    async def reconcile(codes: list[ExtractedCode], transcript: str) -> list[ExtractedCode]:
        called.append(transcript)
        return [ExtractedCode.from_comprehend("I10", None, 0.97)]

    async def store_note(session: Any, *, encounter: Any, note: Any) -> uuid.UUID:
        written.append(note)
        return encounter.id

    from track_a_clinical import notes

    monkeypatch.setattr(notes, "store_note", store_note)

    note = GeneratedNote(
        sections=SoapSections(subjective="s", objective="o", assessment="a", plan="p"),
        icd10_codes=None,
        cpt_codes=None,
    )
    consumer, pubsub = build(Recorder(note), reconcile)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, segment(SESSION_ID, "blood pressure is stable"))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert called == []
    assert written[0].icd10_codes is None


@pytest.mark.asyncio
async def test_a_discovered_code_is_appended_after_the_llm_ones(
    stored: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suggestion never displaces or reorders what the extraction pass proposed."""
    written: list[Any] = []

    async def reconcile(codes: list[ExtractedCode], transcript: str) -> list[ExtractedCode]:
        return [*codes, ExtractedCode.from_comprehend("I10", None, 0.97)]

    async def store_note(session: Any, *, encounter: Any, note: Any) -> uuid.UUID:
        written.append(note)
        return encounter.id

    from track_a_clinical import notes

    monkeypatch.setattr(notes, "store_note", store_note)

    note = GeneratedNote(
        sections=SoapSections(subjective="s", objective="o", assessment="a", plan="p"),
        icd10_codes=[ExtractedCode.from_llm("M17.11")],
        cpt_codes=[],
    )
    consumer, pubsub = build(Recorder(note), reconcile)
    await consumer.handle_message(pubsub, started(SESSION_ID))
    await consumer.handle_message(pubsub, ended(SESSION_ID))
    await drain(consumer)

    assert [code.code for code in written[0].icd10_codes] == ["M17.11", "I10"]
    assert [code.source for code in written[0].icd10_codes] == [
        SOURCE_LLM_EXTRACTION,
        SOURCE_COMPREHEND_MEDICAL,
    ]

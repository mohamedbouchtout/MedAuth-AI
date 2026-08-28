"""Note generation end to end, against real PostgreSQL and Redis.

Skipped when DATABASE_URL is unset, like the rest of this suite, so the unit
tests still run on a machine with no backing services. In CI both are started
from docker-compose and these always run.

What only this suite can prove: that the consumer's real read loop receives what
``audio-ingestion`` and ``POST /sessions/{id}/end`` publish, that the row lands
with the codes in the shape CLAUDE.md fixes, that the audit row lands beside it,
and — the one that matters most — that a ``session:ended`` signal delivered
twice produces exactly one note and exactly one pair of LLM calls.

Bedrock is stubbed. Everything else here is real.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hipaa_logger import close_pool, configure
from track_a_clinical import audit
from track_a_clinical.consumer import TranscriptConsumer
from track_a_clinical.db import database_url
from track_a_clinical.models import (
    ENCOUNTER_STATUS_ACTIVE,
    ClinicalNote,
    Encounter,
    load_codes,
)
from track_a_clinical.soap import GeneratedNote, SoapSections

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL is not set — note generation tests need a real PostgreSQL",
    ),
]

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

#: How long to wait for the consumer's read loop to act on a published message.
#: Generous: it covers a subscribe round trip plus a database write, and a test
#: that fails here has found a real stall rather than a slow machine.
SETTLE_TIMEOUT_SECONDS = 10.0

PATIENT_FHIR_ID = "synthea-placeholder-1"

NOTE = GeneratedNote(
    sections=SoapSections(
        subjective="Right knee pain for six weeks following a fall.",
        objective="Medial joint line tenderness on the right.",
        assessment="Suspected medial meniscal tear, right knee.",
        plan="MRI right knee without contrast.",
    ),
    icd10_codes=[],
    cpt_codes=[],
)


class CountingGenerator:
    """Stands in for soap.generate, counting how often it actually ran."""

    def __init__(self, note: GeneratedNote | None = NOTE) -> None:
        self.note = note
        self.transcripts: list[str] = []

    async def __call__(self, transcript: str, *, session_id: uuid.UUID) -> GeneratedNote | None:
        self.transcripts.append(transcript)
        return self.note

    @property
    def calls(self) -> int:
        return len(self.transcripts)


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory for the consumer and for this suite's own assertions."""
    engine = create_async_engine(database_url())
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    """A client for publishing what audio-ingestion and TASK-006 publish."""
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def encounter(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Encounter]:
    """An active encounter for the consumer to find, cleaned up afterwards."""
    row = Encounter(
        session_id=uuid.uuid4(),
        patient_fhir_id=PATIENT_FHIR_ID,
        provider_id=uuid.uuid4(),
        status=ENCOUNTER_STATUS_ACTIVE,
    )
    async with sessions() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)

    yield row

    async with sessions() as session:
        await session.execute(sa.delete(ClinicalNote).where(ClinicalNote.encounter_id == row.id))
        await session.execute(sa.delete(Encounter).where(Encounter.id == row.id))
        await session.commit()


@pytest_asyncio.fixture
async def consumer(
    redis: Redis,
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[TranscriptConsumer, CountingGenerator]]:
    """The real consumer, on the real Redis, with only Bedrock stubbed out."""
    configure(os.environ["DATABASE_URL"])
    generator = CountingGenerator()

    def factory() -> AsyncSession:
        return sessions()

    running = TranscriptConsumer(
        Redis.from_url(REDIS_URL),
        session_factory=factory,  # type: ignore[arg-type]
        generate=generator,
    )
    running.start()
    await wait_until(running.is_healthy)

    yield running, generator

    await running.stop()
    await close_pool()


async def wait_until(
    predicate: Callable[[], bool], *, within: float = SETTLE_TIMEOUT_SECONDS
) -> None:
    """Poll `predicate` until it holds, failing the test if it never does.

    Polling rather than an event: what is being waited on is the observable
    effect of a background task — a subscription registered, a row committed —
    and the consumer exposes no event for either. Adding one purely so a test
    could await it would put test scaffolding in the service.
    """

    async def poll() -> None:
        while not predicate():  # noqa: ASYNC110 — see the docstring
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(poll(), timeout=within)
    except TimeoutError:  # pragma: no cover - only on a real stall
        pytest.fail(f"condition did not hold within {within}s")


async def announce(redis: Redis, session_id: uuid.UUID) -> None:
    """Publish what POST /sessions/start publishes."""
    await redis.publish("sessions:started", json.dumps({"session_id": str(session_id)}))


async def say(redis: Redis, session_id: uuid.UUID, text: str) -> None:
    """Publish what audio-ingestion publishes for one stabilized segment."""
    await redis.publish(
        f"transcription:{session_id}",
        json.dumps(
            {
                "session_id": str(session_id),
                "result_id": str(uuid.uuid4()),
                "text": text,
                "is_partial": False,
                "start_time": 0.0,
                "end_time": 1.0,
            }
        ),
    )


async def end(redis: Redis, session_id: uuid.UUID) -> None:
    """Publish what POST /sessions/{id}/end publishes — an empty signal."""
    await redis.publish(f"session:ended:{session_id}", "")


async def stored_note(
    sessions: async_sessionmaker[AsyncSession], encounter_id: uuid.UUID
) -> ClinicalNote | None:
    """Read the note back through a fresh session, not the consumer's."""
    async with sessions() as session:
        return await session.scalar(
            sa.select(ClinicalNote).where(ClinicalNote.encounter_id == encounter_id)
        )


async def count_notes(sessions: async_sessionmaker[AsyncSession], encounter_id: uuid.UUID) -> int:
    """Count the notes for one encounter — the invariant TASK-060 depends on."""
    async with sessions() as session:
        result = await session.scalar(
            sa.select(sa.func.count())
            .select_from(ClinicalNote)
            .where(ClinicalNote.encounter_id == encounter_id)
        )
        return int(result or 0)


async def count_audit_rows(
    sessions: async_sessionmaker[AsyncSession], session_id: uuid.UUID, action: str
) -> int:
    """Count audit_log rows hipaa-logger wrote for one session and action."""
    statement = sa.text(
        "SELECT count(*) FROM audit_log WHERE session_id = :sid AND action = :action"
    )
    async with sessions() as session:
        return int(await session.scalar(statement, {"sid": session_id, "action": action}) or 0)


async def run_one_encounter(
    redis: Redis,
    consumer: TranscriptConsumer,
    session_id: uuid.UUID,
    *,
    segments: tuple[str, ...] = ("Right knee pain.", "Order an MRI."),
) -> None:
    """Drive one session from announcement through to its end signal."""
    await announce(redis, session_id)
    await wait_until(lambda: session_id in consumer.watched_sessions)
    for text in segments:
        await say(redis, session_id, text)
    # Let the segments land before the end signal. Both travel the same
    # connection and are handled in order, so this only has to cover the read
    # loop's poll interval.
    await asyncio.sleep(0.2)
    await end(redis, session_id)


async def test_an_ended_session_produces_a_note(
    redis: Redis,
    consumer: tuple[TranscriptConsumer, CountingGenerator],
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    """The task's own acceptance test: buffered transcript in, clinical_notes row out."""
    running, generator = consumer

    await run_one_encounter(redis, running, encounter.session_id)
    await wait_until(lambda: generator.calls == 1)
    await wait_until(lambda: not running.watched_sessions)

    note = await stored_note(sessions, encounter.id)
    assert note is not None
    assert note.soap_assessment == NOTE.sections.assessment
    assert note.soap_plan == NOTE.sections.plan


async def test_the_whole_transcript_reaches_the_generator(
    redis: Redis,
    consumer: tuple[TranscriptConsumer, CountingGenerator],
    encounter: Encounter,
) -> None:
    """Accumulated across segments, not one note per segment."""
    running, generator = consumer

    await run_one_encounter(
        redis, running, encounter.session_id, segments=("First thing.", "Second thing.")
    )
    await wait_until(lambda: generator.calls == 1)
    # Wait for the write too, not just the generation: the buffer is released
    # only after the commit, and the fixture's cleanup deletes the encounter
    # this note points at.
    await wait_until(lambda: not running.watched_sessions)

    assert generator.transcripts == ["First thing. Second thing."]


async def test_the_note_is_audited_as_a_phi_write(
    redis: Redis,
    consumer: tuple[TranscriptConsumer, CountingGenerator],
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    """One WRITE_NOTE row per note, actor taken from the encounter's provider."""
    running, generator = consumer

    await run_one_encounter(redis, running, encounter.session_id)
    await wait_until(lambda: generator.calls == 1)
    await wait_until(lambda: not running.watched_sessions)

    assert await count_audit_rows(sessions, encounter.session_id, audit.ACTION_WRITE_NOTE) == 1

    async with sessions() as session:
        actor = await session.scalar(
            sa.text("SELECT actor_id FROM audit_log WHERE session_id = :sid AND action = :action"),
            {"sid": encounter.session_id, "action": audit.ACTION_WRITE_NOTE},
        )
    assert actor == encounter.provider_id


async def test_a_repeated_end_signal_writes_one_note_and_runs_one_generation(
    redis: Redis,
    consumer: tuple[TranscriptConsumer, CountingGenerator],
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    """Redis pub/sub is not exactly-once, and a second Sonnet call is real money.

    The unique constraint alone would stop the duplicate row but not the
    duplicate pair of LLM calls; the consumer's in-flight guard stops those.
    """
    running, generator = consumer

    await run_one_encounter(redis, running, encounter.session_id)
    await wait_until(lambda: generator.calls == 1)
    await wait_until(lambda: not running.watched_sessions)

    await end(redis, encounter.session_id)
    await asyncio.sleep(0.5)

    assert generator.calls == 1
    assert await count_notes(sessions, encounter.id) == 1
    assert await count_audit_rows(sessions, encounter.session_id, audit.ACTION_WRITE_NOTE) == 1


async def test_the_codes_land_in_the_documented_shape(
    redis: Redis,
    consumer: tuple[TranscriptConsumer, CountingGenerator],
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    """The column round-trips through ExtractedCode, not through bare strings."""
    running, generator = consumer
    generator.note = GeneratedNote(
        sections=NOTE.sections,
        icd10_codes=[
            code
            for code in load_codes(
                [
                    {
                        "code": "M23.221",
                        "display": "Derangement of medial meniscus",
                        "source": "llm-extraction",
                        "confidence": None,
                        "validation": None,
                    }
                ]
            )
        ],
        cpt_codes=[],
    )

    await run_one_encounter(redis, running, encounter.session_id)
    await wait_until(lambda: generator.calls == 1)
    await wait_until(lambda: not running.watched_sessions)

    note = await stored_note(sessions, encounter.id)
    assert note is not None
    codes = load_codes(note.icd10_codes)
    assert [entry.code for entry in codes] == ["M23.221"]
    assert codes[0].confidence is None
    assert codes[0].validation is None, "TASK-031 has not run — not the same as rejected"


async def test_a_failed_extraction_stores_null_codes_not_empty_ones(
    redis: Redis,
    consumer: tuple[TranscriptConsumer, CountingGenerator],
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    """NULL records "never determined"; [] would claim the visit had no codes."""
    running, generator = consumer
    generator.note = GeneratedNote(sections=NOTE.sections, icd10_codes=None, cpt_codes=None)

    await run_one_encounter(redis, running, encounter.session_id)
    await wait_until(lambda: generator.calls == 1)
    await wait_until(lambda: not running.watched_sessions)

    note = await stored_note(sessions, encounter.id)
    assert note is not None
    assert note.icd10_codes is None
    assert note.cpt_codes is None


async def test_a_failed_generation_writes_nothing_and_keeps_the_transcript(
    redis: Redis,
    consumer: tuple[TranscriptConsumer, CountingGenerator],
    sessions: async_sessionmaker[AsyncSession],
    encounter: Encounter,
) -> None:
    """The buffer is what makes the encounter retryable rather than lost."""
    running, generator = consumer
    generator.note = None

    await run_one_encounter(redis, running, encounter.session_id)
    await wait_until(lambda: generator.calls == 1)

    assert await count_notes(sessions, encounter.id) == 0
    assert encounter.session_id in running.watched_sessions

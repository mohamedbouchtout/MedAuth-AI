"""The Track A half of the transcript fan-out (TASK-021, built in TASK-030).

``audio-ingestion`` publishes one message per stabilized transcript segment to
``transcription:{session_id}`` and knows nothing about who reads it. Two
services do, independently: ``track-b-rag`` scans each segment for procedure-
order keywords, and this consumer accumulates the whole encounter and turns it
into a SOAP note when the session ends. Neither is a shared component; they
subscribe to the same channel and never see each other.

**Subscription is per session, never by pattern.** ``transcription:*`` would put
a wildcard across the one channel family that carries speech and hand every
consumer every encounter, so ``POST /sessions/start`` (TASK-006) announces each
new session on the fixed ``sessions:started`` channel and this consumer
subscribes to that session's transcript and end channels by name. The ordering
is safe by construction: the announcement is published before the session JWT is
returned, and no client can open its audio socket without that token, so nothing
can be said before someone is listening. Same shape as
``track_b_rag.transcript_consumer``, deliberately.

**The ordering when a session ends is the load-bearing part.** Unsubscribe
first, so no segment arrives mid-generation and a redelivered end signal finds
nothing to act on. Then hold the buffer across both Bedrock calls and the
database write, and drop it only once the row is committed. Releasing it when
the signal arrived — the way the Track B consumer releases its dedup claim —
would make any failure in between silent and final: the transcript would be
gone, no note would exist, and nothing anywhere would record that an encounter
produced nothing. Holding it means a failed generation is retryable, which is
safe precisely because the write is idempotent (see
:mod:`track_a_clinical.notes`). The same reasoning as TASK-011's
Qdrant-first/Postgres-second ordering, and TASK-060's documented race against
this task depends on the timing here.

**Generation runs off the read loop.** Two LLM calls take seconds, and a pod
serving several providers must not stop accumulating everyone else's transcript
while one note is written. Each generation is its own task, tracked so shutdown
can account for it.

**Known gap: a restart loses the sessions in flight.** The buffers live in this
process, so a redeploy or a dropped Redis connection mid-encounter loses those
transcripts and no note is generated for them — the provider sees a visit that
produced nothing. The consumer logs at WARNING naming the count, which is the
honest version of the problem rather than a fix. Rebuilding would mean querying
``encounters`` for active rows on reconnect, and reconstructing a transcript
that was never persisted is not possible at all: TASK-030 stores no transcript
mid-session by design. ``track_b_rag``'s consumer makes the same trade.

**Segment text is PHI and stays in memory.** It reaches Bedrock through
:mod:`track_a_clinical.soap` and the database as a generated note, and nowhere
else. Log lines here carry session ids and counts, never speech.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from track_a_clinical import comprehend, notes, soap
from track_a_clinical.db import get_sessionmaker
from track_a_clinical.models import ExtractedCode

logger = logging.getLogger(__name__)

#: CLAUDE.md's canonical Redis key list, formatted here rather than at each call
#: site so a variant spelling cannot appear in one place and go unnoticed. Each
#: service formats these patterns locally — the same arrangement
#: ``audio-ingestion`` uses for the publishing side.
SESSIONS_STARTED_CHANNEL: Final = "sessions:started"
TRANSCRIPTION_TEMPLATE: Final = "transcription:{session_id}"
SESSION_ENDED_TEMPLATE: Final = "session:ended:{session_id}"

#: How long a read waits before looping. Not a latency budget — a message
#: arriving mid-wait wakes the read immediately. It only bounds how quickly a
#: cancelled task notices it was cancelled.
_READ_TIMEOUT_SECONDS: Final = 1.0

#: How long to wait before reconnecting after Redis drops the connection. Short:
#: every second of it is a second of a live encounter going unrecorded.
_RECONNECT_DELAY_SECONDS: Final = 2.0

#: Opens the database session a generation writes through. Injected so tests
#: drive the consumer without PostgreSQL, and so the session's lifetime belongs
#: to the generation rather than to the read loop.
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def transcription_channel(session_id: uuid.UUID) -> str:
    """Return the transcript channel for one session."""
    return TRANSCRIPTION_TEMPLATE.format(session_id=session_id)


def session_ended_channel(session_id: uuid.UUID) -> str:
    """Return the end-of-session channel for one session."""
    return SESSION_ENDED_TEMPLATE.format(session_id=session_id)


def _decode(data: object) -> str:
    """Return a Redis payload as text, whatever the client handed back."""
    return data.decode("utf-8") if isinstance(data, bytes) else str(data)


def _session_id_from_channel(channel: str, prefix: str) -> uuid.UUID | None:
    """Return the session id embedded in a channel name, or None if malformed."""
    try:
        return uuid.UUID(channel[len(prefix) :])
    except ValueError:
        logger.warning("Ignoring a message on unparseable channel %r", channel)
        return None


class TranscriptBuffer:
    """One encounter's transcript, accumulated in order and held in memory.

    Segments are kept as received and joined with spaces only when the note is
    generated, so accumulating costs an append rather than a string rebuild per
    segment — a long encounter is thousands of them.

    Never persisted mid-session. TASK-030 accepts losing a visit in flight to a
    restart rather than writing a partial transcript to disk, which would put
    raw encounter speech in a second place for the length of every visit.
    """

    def __init__(self) -> None:
        """Start an empty buffer."""
        self._segments: list[str] = []

    def add(self, text: str) -> None:
        """Append one segment, ignoring an empty one."""
        if text.strip():
            self._segments.append(text.strip())

    @property
    def segment_count(self) -> int:
        """Return how many segments have been accumulated."""
        return len(self._segments)

    def transcript(self) -> str:
        """Return the accumulated transcript as one document."""
        return " ".join(self._segments)


class TranscriptConsumer:
    """Accumulates announced sessions' transcripts and generates a note on end."""

    def __init__(
        self,
        redis: Redis,
        *,
        session_factory: SessionFactory | None = None,
        generate: Callable[..., Awaitable[soap.GeneratedNote | None]] = soap.generate,
        validate_icd10: Callable[
            [list[ExtractedCode], str], Awaitable[list[ExtractedCode]]
        ] = comprehend.validate_icd10,
    ) -> None:
        """Build a consumer.

        Args:
            redis: The client whose pub/sub connection this consumer owns.
            session_factory: Opens the database session each generation writes
                through. Defaults to the service's own sessionmaker.
            generate: The SOAP generation seam. Defaults to the real one.
            validate_icd10: The Comprehend Medical validation seam (TASK-031).
                Defaults to the real one.
        """
        self._redis = redis
        self._session_factory = session_factory or _default_session_factory
        self._generate = generate
        self._validate_icd10 = validate_icd10
        self._buffers: dict[uuid.UUID, TranscriptBuffer] = {}
        self._task: asyncio.Task[None] | None = None
        #: Keyed by session so a second end signal for a session already being
        #: generated is recognised as the duplicate it is. A plain set of tasks
        #: would let a redelivery that beat the unsubscribe spend a second pair
        #: of LLM calls — the unique constraint stops the second *row*, not the
        #: second Sonnet bill.
        self._generations: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._connected = False

    @property
    def watched_sessions(self) -> frozenset[uuid.UUID]:
        """Return the sessions currently being accumulated."""
        return frozenset(self._buffers)

    def is_healthy(self) -> bool:
        """Return whether the consumer is running and subscribed.

        ``GET /health`` reports this. A consumer that has stopped is the reason
        an entire clinic's encounters produce no notes, and that has to be
        visible as something other than a quiet afternoon — TASK-060 would only
        exhaust its retries and log a warning per visit.
        """
        return self._task is not None and not self._task.done() and self._connected

    def start(self) -> None:
        """Launch the read loop as a background task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="track-a-transcript-consumer")

    async def stop(self) -> None:
        """Cancel the read loop and any generation still running.

        A generation cancelled here loses its note: the transcript it was
        working from is in this process and goes with it. That is the restart
        gap in the module docstring, and it is logged rather than papered over.
        """
        if self._generations:
            logger.warning(
                "Shutting down with %d note generation(s) in flight; "
                "their transcripts are in memory and will be lost",
                len(self._generations),
            )
        for generation in list(self._generations.values()):
            generation.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await generation
        self._generations.clear()

        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._connected = False

    async def run(self) -> None:
        """Consume until cancelled, reconnecting when Redis drops the connection."""
        while True:
            try:
                await self._consume()
            except asyncio.CancelledError:
                raise
            except RedisError:
                self._connected = False
                lost = len(self._buffers)
                self._buffers.clear()
                logger.warning(
                    "Transcript consumer lost its Redis connection; %d session(s) "
                    "in flight lost their transcript and will produce no note",
                    lost,
                    exc_info=True,
                )
                await asyncio.sleep(_RECONNECT_DELAY_SECONDS)

    async def _consume(self) -> None:
        """Subscribe to the announcement channel and dispatch messages until failure."""
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(SESSIONS_STARTED_CHANNEL)
            self._connected = True
            logger.info("Transcript consumer subscribed to %s", SESSIONS_STARTED_CHANNEL)
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=_READ_TIMEOUT_SECONDS,
                )
                if message is not None:
                    await self.handle_message(pubsub, message)
        finally:
            self._connected = False
            with contextlib.suppress(Exception):
                # redis-py ships no annotation for this one; every other call
                # on the subscription type-checks.
                await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def handle_message(self, pubsub: Any, message: dict[str, Any]) -> None:
        """Route one pub/sub message to the handler for its channel.

        Args:
            pubsub: The subscription, so a session-start message can add
                subscriptions and a session-end message can drop them.
            message: A redis-py message mapping.
        """
        channel = _decode(message.get("channel"))
        data = message.get("data")

        if channel == SESSIONS_STARTED_CHANNEL:
            await self._on_session_started(pubsub, _decode(data))
        elif channel.startswith("transcription:"):
            self._on_segment(channel, _decode(data))
        elif channel.startswith("session:ended:"):
            await self._on_session_ended(pubsub, channel)
        else:
            logger.warning("Transcript consumer ignored a message on %r", channel)

    async def _on_session_started(self, pubsub: Any, payload: str) -> None:
        """Subscribe to a newly announced session and open a buffer for it."""
        try:
            session_id = uuid.UUID(json.loads(payload)["session_id"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            logger.warning("Ignoring an unreadable session-started announcement")
            return

        if session_id in self._buffers:
            # A redelivered announcement, not a second visit: session ids are
            # server-generated UUIDs. Re-opening the buffer would discard
            # whatever had already been said.
            logger.info("Session %s is already being accumulated", session_id)
            return

        await pubsub.subscribe(transcription_channel(session_id), session_ended_channel(session_id))
        self._buffers[session_id] = TranscriptBuffer()
        logger.info("Transcript consumer accumulating session %s", session_id)

    def _on_segment(self, channel: str, payload: str) -> None:
        """Append one transcript segment to its session's buffer."""
        session_id = _session_id_from_channel(channel, "transcription:")
        if session_id is None:
            return

        buffer = self._buffers.get(session_id)
        if buffer is None:
            # A segment for a session this consumer never heard start, or one
            # already ended. Nothing to accumulate it into, and inventing a
            # buffer would generate a note from a fragment.
            logger.warning("Dropping a segment for unwatched session %s", session_id)
            return

        try:
            segment = json.loads(payload)
            text = segment["text"]
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            logger.warning("Ignoring an unreadable transcript segment on session %s", session_id)
            return

        if segment.get("is_partial"):
            # audio-ingestion already drops partials; this is the belt to that
            # braces. A partial is revised repeatedly under one result id, so
            # accumulating them would repeat every sentence several times over.
            return

        buffer.add(str(text))

    async def _on_session_ended(self, pubsub: Any, channel: str) -> None:
        """Unsubscribe from a finished session and start generating its note.

        Unsubscribe happens here, synchronously, before anything else: it is
        what stops a late segment landing mid-generation and what makes a
        redelivered end signal a no-op. The buffer is deliberately *not*
        dropped — see the module docstring.
        """
        session_id = _session_id_from_channel(channel, "session:ended:")
        if session_id is None:
            return

        await pubsub.unsubscribe(
            transcription_channel(session_id), session_ended_channel(session_id)
        )

        if session_id in self._generations:
            # A redelivery that arrived before the unsubscribe took effect. The
            # note is already being generated from this same buffer; starting a
            # second generation would pay for another Sonnet and another Haiku
            # call to produce a row the unique constraint would then discard.
            logger.info("Session %s is already generating its note", session_id)
            return

        buffer = self._buffers.get(session_id)
        if buffer is None:
            # The signal arrived twice, or for a session this consumer never
            # watched. Either way there is nothing here to generate from, and
            # the note for the first delivery is already stored.
            logger.info("No transcript held for session %s; nothing to generate", session_id)
            return

        logger.info(
            "Session %s ended with %d segment(s); generating its note",
            session_id,
            buffer.segment_count,
        )
        task = asyncio.create_task(
            self._generate_and_store(session_id, buffer),
            name=f"soap-generation-{session_id}",
        )
        self._generations[session_id] = task
        task.add_done_callback(lambda _: self._generations.pop(session_id, None))

    async def _validate_codes(
        self,
        note: soap.GeneratedNote,
        transcript: str,
        session_id: uuid.UUID,
    ) -> soap.GeneratedNote:
        """Return `note` with its ICD-10 codes validated, or unchanged on failure.

        **Runs before the insert, deliberately.** ``store_note`` is
        ``ON CONFLICT DO NOTHING`` and has no idempotent update path, so a
        validation pass running after the write would have nothing to update on
        the retry of a duplicated signal. Validating first means the row is
        written already validated, in one write, with no second transaction to
        get half-applied.

        **Never fatal.** Any failure leaves every ``validation`` at ``None`` and
        the note is stored as it stands — the same independence TASK-030 gives
        the Sonnet and Haiku passes, with the note as the higher-priority
        artifact and validation as metadata about it. ``None`` already reads as
        "not checked yet", so nothing further is needed to represent it
        honestly.

        ``cpt_codes`` are not touched: Comprehend Medical has no CPT inference,
        so those entries keep ``validation: None`` permanently by design.
        """
        if not note.icd10_codes:
            return note
        try:
            validated = await self._validate_icd10(note.icd10_codes, transcript)
        except Exception:
            logger.warning(
                "ICD-10 validation failed for session %s; storing the note unvalidated",
                session_id,
                exc_info=True,
            )
            return note
        return replace(note, icd10_codes=validated)

    async def _generate_and_store(self, session_id: uuid.UUID, buffer: TranscriptBuffer) -> None:
        """Generate the note for one ended session and store it, then free the buffer.

        The buffer is released in exactly one place — after the row is committed
        — so every failure path above leaves it held and the encounter
        retryable. Nothing retries automatically today; what this buys is that a
        retry is *possible*, and that a lost note is a logged failure rather
        than a transcript that quietly no longer exists.
        """
        try:
            transcript = buffer.transcript()
            note = await self._generate(transcript, session_id=session_id)
            if note is None:
                # soap.generate has already logged which pass failed. The buffer
                # stays; nothing here can improve on the attempt.
                return

            note = await self._validate_codes(note, transcript, session_id)

            async with self._session_factory() as session:
                encounter = await notes.load_encounter(session, session_id)
                if encounter is None:
                    logger.error(
                        "No encounter for session %s; its note cannot be stored",
                        session_id,
                    )
                    return
                await notes.store_note(session, encounter=encounter, note=note)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Storing the note for session %s failed; its transcript is still held",
                session_id,
                exc_info=True,
            )
            return

        # Committed. This is the only place a buffer is dropped on the success
        # path, and it is deliberately after the write rather than before it.
        self._buffers.pop(session_id, None)


@asynccontextmanager
async def _default_session_factory() -> AsyncIterator[AsyncSession]:
    """Yield a database session for one generation, closing it afterwards.

    Closing rolls back any transaction still open, which is the safety net for a
    generation that raised between the insert and the commit.
    """
    async with get_sessionmaker()() as session:
        yield session

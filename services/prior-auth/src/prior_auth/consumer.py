"""The prior-auth half of the session-end fan-out (TASK-060).

``track-a-clinical`` publishes an empty-payload signal to
``session:ended:{session_id}`` when a provider ends a visit, and knows nothing
about who reads it. Two services do, independently: that service generates the
SOAP note, and this one assembles a prior-authorization bundle from what the
encounter produced. Neither is a shared component; they subscribe to the same
channel and never see each other.

**Subscription is per session, never by pattern.** ``session:ended:*`` would put
a wildcard across a channel family keyed on live visits, so
``POST /sessions/start`` announces each new session on the fixed
``sessions:started`` channel and this consumer subscribes to that session's end
channel by name. Same shape as the two transcript consumers, deliberately. This
one does not subscribe to ``transcription:{session_id}`` at all: it reads
nothing from the transcript, by the decision recorded in
:mod:`prior_auth.evidence`.

The order of work on an end signal
----------------------------------
Unsubscribe first, so a redelivered signal finds nothing to act on. Then, in
this order, because each step can rule the assembly out more cheaply than the
one after it:

1. **Load the encounter.** No encounter, nothing to assemble.
2. **Check ``launch_id``.** A visit started outside a SMART launch has none, so
   the patient's chart context is unreachable and the request could never be
   submitted. Write nothing, and log naming ``ENCOUNTER_NOT_LINKED_TO_EHR`` —
   TASK-053's vocabulary for the same underlying situation. That task raises it
   as a 422 because it has a client to answer; there is no request behind this
   one, so the name is what carries across and the surface cannot. **This is a
   settled fact on the first read, so it never enters the backoff below.**
3. **Check for a coded procedure.** Most encounters raise no nudge at all, and
   there is nothing to ask a payer to authorize. Ruled out before the backoff,
   so an ordinary visit does not spend fourteen seconds waiting for a note it
   has no use for.
4. **Wait for the note.** TASK-060's documented race: the end signal reaches
   both services at once and the SOAP pass is two Bedrock calls plus a
   Comprehend Medical call, so the note routinely does not exist yet. Retried
   with backoff, and giving up is a warning rather than silence.
5. **Read the chart context**, which also establishes that the EHR still
   resolves this patient under this launch.
6. **Write the row**, once, idempotently.

Nothing is written before step 6, so every failure above leaves no partial
state and the assembly is retryable. Ordering it the other way — a row first,
enriched later — would leave rows on TASK-072's dashboard that look like pending
requests and are not.

**Nothing here logs clinical content.** Log lines carry session and encounter
ids and counts, never a procedure, a diagnosis, a criterion or a note excerpt.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from prior_auth import assembly, patient_context
from prior_auth.config import get_settings
from prior_auth.db import get_sessionmaker

logger = logging.getLogger(__name__)

#: CLAUDE.md's canonical Redis key list, formatted here rather than at each call
#: site so a variant spelling cannot appear in one place and go unnoticed. Each
#: service formats these patterns locally, as track-a-clinical does.
SESSIONS_STARTED_CHANNEL: Final = "sessions:started"
SESSION_ENDED_TEMPLATE: Final = "session:ended:{session_id}"

#: TASK-053's error code for a visit with no EHR chart entry behind it. Reused
#: verbatim so an operator grepping either service finds one name for one
#: situation — see the module docstring for why only the name carries across.
ENCOUNTER_NOT_LINKED_TO_EHR: Final = "ENCOUNTER_NOT_LINKED_TO_EHR"

#: How long a read waits before looping. Not a latency budget — a message
#: arriving mid-wait wakes the read immediately. It only bounds how quickly a
#: cancelled task notices it was cancelled.
_READ_TIMEOUT_SECONDS: Final = 1.0

#: How long to wait before reconnecting after Redis drops the connection.
_RECONNECT_DELAY_SECONDS: Final = 2.0

#: Opens the database session an assembly runs in. Injected so tests drive the
#: consumer without PostgreSQL, and so the session's lifetime belongs to the
#: assembly rather than to the read loop.
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


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


class SessionEndConsumer:
    """Assembles a prior-authorization bundle when an announced session ends."""

    def __init__(
        self,
        redis: Redis,
        *,
        session_factory: SessionFactory | None = None,
        fetch_context: Callable[..., Any] = patient_context.fetch_patient_context,
        retry_delays: tuple[float, ...] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        """Build a consumer.

        Args:
            redis: The client whose pub/sub connection this consumer owns.
            session_factory: Opens the database session each assembly runs in.
                Defaults to the service's own sessionmaker.
            fetch_context: The chart-context seam. Defaults to the real one.
            retry_delays: Waits between note-load attempts. Defaults to the
                configured value, so a test can drive the retry path without
                spending fourteen seconds on it.
            sleep: The wait seam, injected for the same reason.
        """
        self._redis = redis
        self._session_factory = session_factory or _default_session_factory
        self._fetch_context = fetch_context
        self._retry_delays = (
            retry_delays if retry_delays is not None else get_settings().note_retry_delays
        )
        self._sleep = sleep
        self._watched: set[uuid.UUID] = set()
        self._task: asyncio.Task[None] | None = None
        #: Keyed by session so a second end signal for a session already being
        #: assembled is recognised as the duplicate it is. The unique constraint
        #: stops the second *row*; this stops the second fourteen-second wait
        #: and the second chart read that would precede it.
        self._assemblies: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._connected = False

    @property
    def watched_sessions(self) -> frozenset[uuid.UUID]:
        """Return the sessions this consumer is waiting on the end of."""
        return frozenset(self._watched)

    def is_healthy(self) -> bool:
        """Return whether the consumer is running and subscribed.

        ``GET /health`` reports this. A consumer that has stopped means no
        encounter on this pod produces a prior-authorization request, and
        nothing else in the system notices — the provider sees a visit that
        flagged procedures and filed nothing.
        """
        return self._task is not None and not self._task.done() and self._connected

    def start(self) -> None:
        """Launch the read loop as a background task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="prior-auth-session-end-consumer")

    async def stop(self) -> None:
        """Cancel the read loop and any assembly still running.

        An assembly cancelled here writes no row, and the encounter's note and
        nudges are still in the database — so unlike a cancelled SOAP
        generation, nothing is lost that cannot be rebuilt. Nothing retries it
        automatically today, which is why it is logged.
        """
        if self._assemblies:
            logger.warning(
                "Shutting down with %d bundle assembly(ies) in flight; their "
                "encounters will have no prior-authorization request",
                len(self._assemblies),
            )
        for task in list(self._assemblies.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._assemblies.clear()

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
                lost = len(self._watched)
                self._watched.clear()
                logger.warning(
                    "Session-end consumer lost its Redis connection; %d session(s) "
                    "in flight will produce no prior-authorization request",
                    lost,
                    exc_info=True,
                )
                await self._sleep(_RECONNECT_DELAY_SECONDS)

    async def _consume(self) -> None:
        """Subscribe to the announcement channel and dispatch until failure."""
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(SESSIONS_STARTED_CHANNEL)
            self._connected = True
            logger.info("Session-end consumer subscribed to %s", SESSIONS_STARTED_CHANNEL)
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
            pubsub: The subscription, so a session-start message can add a
                subscription and a session-end message can drop it.
            message: A redis-py message mapping.
        """
        channel = _decode(message.get("channel"))

        if channel == SESSIONS_STARTED_CHANNEL:
            await self._on_session_started(pubsub, _decode(message.get("data")))
        elif channel.startswith("session:ended:"):
            await self._on_session_ended(pubsub, channel)
        else:
            logger.warning("Session-end consumer ignored a message on %r", channel)

    async def _on_session_started(self, pubsub: Any, payload: str) -> None:
        """Subscribe to a newly announced session's end channel."""
        try:
            session_id = uuid.UUID(json.loads(payload)["session_id"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            logger.warning("Ignoring an unreadable session-started announcement")
            return

        if session_id in self._watched:
            # A redelivered announcement, not a second visit: session ids are
            # server-generated UUIDs.
            logger.info("Session %s is already being watched", session_id)
            return

        await pubsub.subscribe(session_ended_channel(session_id))
        self._watched.add(session_id)
        logger.info("Session-end consumer watching session %s", session_id)

    async def _on_session_ended(self, pubsub: Any, channel: str) -> None:
        """Unsubscribe from a finished session and start assembling its bundle."""
        session_id = _session_id_from_channel(channel, "session:ended:")
        if session_id is None:
            return

        await pubsub.unsubscribe(session_ended_channel(session_id))
        self._watched.discard(session_id)

        if session_id in self._assemblies:
            # A redelivery that arrived before the unsubscribe took effect.
            logger.info("Session %s is already assembling its bundle", session_id)
            return

        logger.info("Session %s ended; assembling its prior-authorization bundle", session_id)
        task = asyncio.create_task(
            self._assemble(session_id),
            name=f"prior-auth-assembly-{session_id}",
        )
        self._assemblies[session_id] = task
        task.add_done_callback(lambda _: self._assemblies.pop(session_id, None))

    async def _await_note(
        self,
        session: AsyncSession,
        *,
        encounter: Any,
        session_id: uuid.UUID,
    ) -> Any:
        """Load the encounter's note, retrying while TASK-030 is still generating it.

        One more attempt than there are delays: the first look costs nothing and
        usually happens after generation, since a note takes seconds and this
        path has already done three queries.

        Returns:
            The note, or None once the attempts are exhausted.
        """
        for attempt, delay in enumerate((*self._retry_delays, None), start=1):
            note = await assembly.load_note(session, encounter=encounter)
            if note is not None:
                if attempt > 1:
                    logger.info("Note for session %s appeared on attempt %d", session_id, attempt)
                return note
            if delay is None:
                break
            # Expire the identity map before sleeping. Without this the session
            # would answer the next attempt from the snapshot it already has and
            # never see the row TASK-030 committed in the meantime, which would
            # make the whole backoff decorative.
            await session.rollback()
            await self._sleep(delay)
        return None

    async def _assemble(self, session_id: uuid.UUID) -> None:
        """Assemble and store one ended session's bundle. See the module docstring."""
        try:
            async with self._session_factory() as session:
                encounter = await assembly.load_encounter(session, session_id)
                if encounter is None:
                    logger.error(
                        "No encounter for session %s; no prior-authorization bundle "
                        "can be assembled",
                        session_id,
                    )
                    return

                if not encounter.launch_id:
                    # Settled, not transient: this never enters the backoff.
                    logger.warning(
                        "Encounter %s has no SMART launch behind it (%s), so its "
                        "patient's chart context is unreachable and any request "
                        "would be unsubmittable; assembling no bundle",
                        encounter.id,
                        ENCOUNTER_NOT_LINKED_TO_EHR,
                    )
                    return

                nudges = await assembly.load_nudges(session, encounter=encounter)
                if not any((nudge.cpt_code or "").strip() for nudge in nudges):
                    # The ordinary case for most visits, so INFO rather than
                    # WARNING: nothing was flagged that a payer could be asked
                    # to authorize.
                    logger.info(
                        "Encounter %s fired no nudge carrying a CPT code; there is "
                        "nothing to request authorization for",
                        encounter.id,
                    )
                    return

                note = await self._await_note(session, encounter=encounter, session_id=session_id)
                if note is None:
                    logger.warning(
                        "No clinical note for encounter %s after %d attempt(s); "
                        "assembling no bundle. Its nudges and codes are still stored, "
                        "so this encounter can be assembled again once the note exists",
                        encounter.id,
                        len(self._retry_delays) + 1,
                    )
                    return

                context = await self._fetch_context(
                    launch_id=encounter.launch_id,
                    patient_id=encounter.patient_fhir_id,
                )
                if context is None:
                    # fetch_patient_context has already logged at ERROR naming
                    # the patient. Nothing is written, so a retry is clean.
                    return

                bundle = assembly.build_bundle(
                    note=note,
                    nudges=nudges,
                    encounter_payer=encounter.insurance_payer,
                    coverage_payer=context.coverage.payer if context.coverage else None,
                )
                if bundle.uncoded_nudges:
                    logger.info(
                        "Encounter %s: %d nudge(s) carried no CPT code and are not "
                        "in the bundle — a payer cannot be asked to authorize an "
                        "uncoded procedure",
                        encounter.id,
                        bundle.uncoded_nudges,
                    )
                if bundle.payer_name is None:
                    # Not fatal here. The submission route refuses a request it
                    # cannot make conformant, with a named error a human can
                    # act on, and the assembled evidence is worth keeping either
                    # way — discarding it would lose the encounter's findings to
                    # a gap a provider can still fill.
                    logger.warning(
                        "Encounter %s has no payer name from either the encounter or "
                        "the chart context; the bundle is stored but cannot be "
                        "submitted until one is supplied",
                        encounter.id,
                    )

                await assembly.store_bundle(session, encounter=encounter, bundle=bundle)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Assembling the prior-authorization bundle for session %s failed; "
                "no request was recorded",
                session_id,
                exc_info=True,
            )


@asynccontextmanager
async def _default_session_factory() -> AsyncIterator[AsyncSession]:
    """Yield a database session for one assembly, closing it afterwards.

    Closing rolls back any transaction still open, which is the safety net for
    an assembly that raised between the insert and the commit.
    """
    async with get_sessionmaker()() as session:
        yield session

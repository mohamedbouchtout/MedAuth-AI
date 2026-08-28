"""The Track B half of the transcript fan-out (TASK-021).

``audio-ingestion`` publishes one message per stabilized transcript segment to
``transcription:{session_id}`` and knows nothing about who reads it. Two
services do, independently: ``track-a-clinical`` accumulates the transcript for
SOAP generation (TASK-030), and this consumer scans it for procedure-order
keywords. Neither is a shared component; they subscribe to the same channel and
never see each other.

**Subscription is per session, and that needs a session-start signal.** The
alternative is pattern-subscribing ``transcription:*``, which puts a wildcard
across the one channel family that carries PHI and hands every consumer every
session's speech. So ``POST /sessions/start`` (TASK-006) announces each new
session on the single fixed ``sessions:started`` channel, and this consumer
subscribes to that one session's transcript channel and its end signal. The
ordering is safe by construction: the announcement is published before the
session JWT is returned, and no client can open the audio socket without that
token, so nothing can be said before someone is listening.

**Repeat mentions are suppressed for the life of the encounter.** See
:mod:`track_b_rag.dedup`; the claim is taken here, immediately before the query,
and given back only when the query failed in a way that could succeed next time.

**Known gap: a restart loses the sessions in flight.** Watched sessions live in
this process, so a redeploy or a dropped Redis connection mid-encounter leaves
those visits unwatched until they end — no keyword is detected and no nudge is
raised, and the provider sees a quiet visit. The consumer logs at WARNING when
it happens, which is the honest version of the problem rather than a fix.
Rebuilding the watch set would mean querying ``encounters`` for active rows on
reconnect; that is real work with its own failure modes, and it belongs with the
task that makes the rest of this path produce actual queries. TASK-030 makes the
same trade for its transcript buffer, deliberately.

**PHI discipline.** Segment text reaches this module and leaves it inside
``clinical_context``. It is never logged: log lines here carry session ids and
canonical keywords, which are identifiers and vocabulary, never speech.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from track_b_rag import dedup, keywords, nudges, policy_dispatch
from track_b_rag.keywords import ProcedureMention
from track_b_rag.policy_dispatch import (
    MissingQueryParameters,
    PolicyQueryOutcome,
    resolve_and_query_policy,
)

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
#: every second of it is a second of a live encounter going unwatched.
_RECONNECT_DELAY_SECONDS: Final = 2.0

#: What the consumer sends as ``clinical_context``.
#:
#: **Only clinical text belongs in here.** Stage 2's matcher flattens every
#: value in this mapping into a term vocabulary and keeps digits of any length,
#: because a criterion's numbers usually are the criterion — "six weeks of
#: conservative therapy". Putting the segment's timing or its result id in
#: would feed stray digits into that vocabulary, and a criterion reading "6
#: weeks" could match a timestamp. So the excerpt travels alone.
_CONTEXT_KEY: Final = "transcript_excerpt"

#: What :func:`resolve_and_query_policy` is called through. Injected so tests
#: drive the consumer without a policy query, and so TASK-024 can change what
#: happens behind the seam without touching this module.
Dispatch = Callable[..., Awaitable[PolicyQueryOutcome]]

#: What :func:`nudges.emit` is called through, injected for the same reason.
Emit = Callable[..., Awaitable[uuid.UUID | None]]


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


class TranscriptConsumer:
    """Watches announced sessions and turns procedure mentions into policy queries."""

    def __init__(
        self,
        redis: Redis,
        *,
        dispatch: Dispatch = resolve_and_query_policy,
        emit: Emit = nudges.emit,
    ) -> None:
        """Build a consumer.

        Args:
            redis: The client whose pub/sub connection this consumer owns.
            dispatch: The policy-query seam. Defaults to the real one.
            emit: The nudge emitter. Defaults to the real one.
        """
        self._redis = redis
        self._dispatch = dispatch
        self._emit = emit
        self._watched: set[uuid.UUID] = set()
        self._task: asyncio.Task[None] | None = None
        self._connected = False

    @property
    def watched_sessions(self) -> frozenset[uuid.UUID]:
        """Return the sessions currently subscribed to."""
        return frozenset(self._watched)

    def is_healthy(self) -> bool:
        """Return whether the consumer is running and subscribed.

        ``GET /health`` reports this. A consumer that has stopped is the reason a
        whole clinic sees no nudges, and that has to be visible as something
        other than a quiet afternoon.
        """
        return self._task is not None and not self._task.done() and self._connected

    def start(self) -> None:
        """Launch the read loop as a background task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="transcript-consumer")

    async def stop(self) -> None:
        """Cancel the read loop and wait for it to unwind."""
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
                    "Transcript consumer lost its Redis connection; %d session(s) "
                    "in flight are no longer watched and will raise no nudges",
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

    async def handle_message(self, pubsub: Any, message: Mapping[str, Any]) -> None:
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
            await self._on_segment(channel, _decode(data))
        elif channel.startswith("session:ended:"):
            await self._on_session_ended(pubsub, channel)
        else:
            logger.warning("Transcript consumer ignored a message on %r", channel)

    async def _on_session_started(self, pubsub: Any, payload: str) -> None:
        """Subscribe to a newly announced session's transcript and end channels."""
        try:
            session_id = uuid.UUID(json.loads(payload)["session_id"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            logger.warning("Ignoring an unreadable session-started announcement")
            return

        if session_id in self._watched:
            # A redelivered announcement, not a second visit: session ids are
            # server-generated UUIDs. Subscribing twice would double every
            # segment and defeat nothing but the dedup guard's usefulness.
            logger.info("Session %s is already watched", session_id)
            return

        await pubsub.subscribe(transcription_channel(session_id), session_ended_channel(session_id))
        self._watched.add(session_id)
        logger.info("Transcript consumer watching session %s", session_id)

    async def _on_session_ended(self, pubsub: Any, channel: str) -> None:
        """Unsubscribe from a finished session and drop its dedup guard."""
        session_id = _session_id_from_channel(channel, "session:ended:")
        if session_id is None:
            return

        await pubsub.unsubscribe(
            transcription_channel(session_id), session_ended_channel(session_id)
        )
        self._watched.discard(session_id)
        await dedup.forget_session(self._redis, session_id)
        logger.info("Transcript consumer released session %s", session_id)

    async def _on_segment(self, channel: str, payload: str) -> None:
        """Scan one transcript segment and query for each new procedure in it."""
        session_id = _session_id_from_channel(channel, "transcription:")
        if session_id is None:
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
            # scanning them would fire the same keyword several times a second.
            return

        for mention in keywords.detect_procedures(text):
            await self._query_for(session_id, mention)

    async def _query_for(self, session_id: uuid.UUID, mention: ProcedureMention) -> None:
        """Run the policy query for one mention, honouring the once-per-session guard.

        The guard is claimed on the CPT code where one resolves and on the
        keyword where it does not (:func:`policy_dispatch.procedure_key`), so
        that two keywords naming one procedure hold one claim between them and
        raise one nudge rather than two.
        """
        key = policy_dispatch.procedure_key(mention)
        if not await dedup.claim_procedure(self._redis, session_id, key):
            logger.debug(
                "Suppressed a repeat mention of %r in session %s", mention.keyword, session_id
            )
            return

        try:
            outcome = await self._dispatch(
                session_id=session_id,
                mention=mention,
                clinical_context={_CONTEXT_KEY: mention.excerpt},
            )
            if outcome.answer is not None:
                # Inside the claim deliberately. The store-then-publish pair is
                # what the claim protects, and a failure anywhere in it lands
                # in the handler below, which gives the claim back so the next
                # mention retries. The retry cannot duplicate the row: the
                # insert names migration 0005's unique index and republishes
                # what it finds.
                await self._emit(
                    redis=self._redis,
                    session_id=session_id,
                    parameters=outcome.parameters,
                    answer=outcome.answer,
                )
        except MissingQueryParameters as missing:
            # Structural, not transient: the claim is kept deliberately, so this
            # is logged once per procedure per session instead of once per
            # segment that names it.
            logger.warning(
                "No policy query for %r in session %s — no source yet for %s%s",
                mention.keyword,
                session_id,
                ", ".join(missing.fields),
                f" ({missing.reason})" if missing.reason else "",
            )
        except Exception:
            # Something that might work next time. Give the claim back so a
            # later mention of the same procedure gets another attempt rather
            # than being silently suppressed for the rest of the encounter.
            await dedup.release_procedure(self._redis, session_id, key)
            logger.error(
                "Policy query or nudge failed for %r in session %s",
                mention.keyword,
                session_id,
                exc_info=True,
            )

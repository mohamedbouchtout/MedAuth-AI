"""The session-end consumer: what it subscribes to, and when it assembles.

Driven through ``handle_message`` with a fake subscription and a fake database
session, so the whole decision path runs without Redis or PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from prior_auth import assembly
from prior_auth import consumer as consumer_module
from prior_auth.consumer import SessionEndConsumer
from prior_auth.patient_context import CoverageInfo, PatientContext
from tests.unit.factories import a_note, a_nudge, an_encounter


class FakePubSub:
    """Records subscribe/unsubscribe rather than talking to Redis."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.unsubscribed.extend(channels)


class FakeSession:
    """Serves the three loads by type, and records the insert."""

    def __init__(
        self,
        *,
        encounter: Any,
        nudges: list[Any],
        notes: list[Any],
    ) -> None:
        self.encounter = encounter
        self.nudges = nudges
        #: Popped left to right, so a test can say "absent, absent, then there"
        #: and drive the backoff.
        self.notes = list(notes)
        self.stored: list[Any] = []
        self.rollbacks = 0

    async def scalar(self, statement: Any) -> Any:
        entity = statement.column_descriptions[0]["entity"].__name__
        if entity == "Encounter":
            return self.encounter
        if entity == "ClinicalNote":
            return self.notes.pop(0) if self.notes else None
        raise AssertionError(f"unexpected scalar for {entity}")

    async def scalars(self, statement: Any) -> Any:
        del statement
        return iter(self.nudges)

    async def rollback(self) -> None:
        self.rollbacks += 1


def a_context(payer: str | None = "Aetna") -> PatientContext:
    return PatientContext(coverage=CoverageInfo(payer=payer))


def build(
    session: FakeSession | None,
    *,
    context: PatientContext | None = None,
    retry_delays: tuple[float, ...] = (),
) -> tuple[SessionEndConsumer, list[float]]:
    """Return a consumer wired to fakes, and the record of what it slept."""
    slept: list[float] = []

    @asynccontextmanager
    async def factory() -> Any:
        yield session

    async def fetch_context(**kwargs: Any) -> PatientContext | None:
        del kwargs
        return context

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    consumer = SessionEndConsumer(
        redis=None,  # type: ignore[arg-type]
        session_factory=factory,
        fetch_context=fetch_context,
        retry_delays=retry_delays,
        sleep=sleep,
    )

    return consumer, slept


@pytest.fixture(autouse=True)
def _stub_store(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the real insert; ``test_assembly`` covers it directly."""
    calls: list[dict[str, Any]] = []

    async def fake_store(_session: Any, *, encounter: Any, bundle: Any) -> uuid.UUID:
        calls.append({"encounter": encounter, "bundle": bundle})
        return uuid.uuid4()

    monkeypatch.setattr(assembly, "store_bundle", fake_store)
    return calls


async def end(consumer: SessionEndConsumer, session_id: uuid.UUID, pubsub: FakePubSub) -> None:
    """Deliver a session-end signal and wait for the assembly it starts."""
    await consumer.handle_message(pubsub, {"channel": f"session:ended:{session_id}", "data": ""})
    for task in list(consumer._assemblies.values()):  # noqa: SLF001
        await task


class TestSubscription:
    async def test_an_announced_session_is_watched_by_name(self) -> None:
        """Never a pattern subscription over the live-visit channel family."""
        consumer, _ = build(None)
        pubsub = FakePubSub()
        session_id = uuid.uuid4()

        await consumer.handle_message(
            pubsub,
            {"channel": "sessions:started", "data": json.dumps({"session_id": str(session_id)})},
        )

        assert pubsub.subscribed == [f"session:ended:{session_id}"]
        assert consumer.watched_sessions == frozenset({session_id})

    async def test_a_redelivered_announcement_does_not_subscribe_twice(self) -> None:
        consumer, _ = build(None)
        pubsub = FakePubSub()
        payload = json.dumps({"session_id": str(uuid.uuid4())})

        for _ in range(2):
            await consumer.handle_message(pubsub, {"channel": "sessions:started", "data": payload})

        assert len(pubsub.subscribed) == 1

    async def test_an_unreadable_announcement_is_ignored(self) -> None:
        consumer, _ = build(None)
        pubsub = FakePubSub()

        await consumer.handle_message(pubsub, {"channel": "sessions:started", "data": "{"})

        assert pubsub.subscribed == []

    async def test_a_message_on_an_unknown_channel_is_ignored(self, caplog: Any) -> None:
        consumer, _ = build(None)
        with caplog.at_level(logging.WARNING):
            await consumer.handle_message(FakePubSub(), {"channel": "nudges:x", "data": ""})
        assert "ignored a message" in caplog.text

    async def test_an_unparseable_end_channel_is_ignored(self, caplog: Any) -> None:
        consumer, _ = build(None)
        with caplog.at_level(logging.WARNING):
            await consumer.handle_message(
                FakePubSub(), {"channel": "session:ended:not-a-uuid", "data": ""}
            )
        assert "unparseable channel" in caplog.text

    async def test_ending_unsubscribes_before_assembling(self) -> None:
        session_id = uuid.uuid4()
        encounter = an_encounter(session_id=session_id)
        session = FakeSession(encounter=encounter, nudges=[a_nudge()], notes=[a_note()])
        consumer, _ = build(session, context=a_context())
        pubsub = FakePubSub()

        await end(consumer, session_id, pubsub)

        assert pubsub.unsubscribed == [f"session:ended:{session_id}"]
        assert consumer.watched_sessions == frozenset()


class TestTheHappyPath:
    async def test_a_bundle_is_assembled_from_the_note_and_the_nudges(
        self, _stub_store: list[dict[str, Any]]
    ) -> None:
        encounter = an_encounter()
        session = FakeSession(
            encounter=encounter,
            nudges=[a_nudge(missing_criteria=["six weeks of conservative therapy"])],
            notes=[a_note()],
        )
        consumer, _ = build(session, context=a_context())

        await end(consumer, encounter.session_id, FakePubSub())

        bundle = _stub_store[0]["bundle"]
        assert bundle.procedures == [{"cpt_code": "73721", "description": "knee MRI"}]
        assert bundle.payer_name == "Aetna"
        assert any(
            "six weeks of physical therapy" in entry["text"] for entry in bundle.clinical_evidence
        )


class TestRulingTheAssemblyOut:
    async def test_no_encounter_assembles_nothing(
        self, _stub_store: list[dict[str, Any]], caplog: Any
    ) -> None:
        session = FakeSession(encounter=None, nudges=[], notes=[])
        consumer, _ = build(session)

        with caplog.at_level(logging.ERROR):
            await end(consumer, uuid.uuid4(), FakePubSub())

        assert _stub_store == []
        assert "No encounter for session" in caplog.text

    async def test_a_visit_with_no_launch_writes_no_row(
        self, _stub_store: list[dict[str, Any]], caplog: Any
    ) -> None:
        """TASK-053's vocabulary, reused; its 422 surface cannot be.

        A bundle with no chart context looks like an ordinary pending request on
        the dashboard and is unsubmittable at the payer.
        """
        encounter = an_encounter(launch_id=None)
        session = FakeSession(encounter=encounter, nudges=[a_nudge()], notes=[a_note()])
        consumer, slept = build(session, context=a_context())

        with caplog.at_level(logging.WARNING):
            await end(consumer, encounter.session_id, FakePubSub())

        assert _stub_store == []
        assert "ENCOUNTER_NOT_LINKED_TO_EHR" in caplog.text

    async def test_the_launch_check_never_enters_the_backoff(self) -> None:
        """A settled fact on the first read, not something waiting would change."""
        encounter = an_encounter(launch_id=None)
        session = FakeSession(encounter=encounter, nudges=[a_nudge()], notes=[])
        consumer, slept = build(session, retry_delays=(2.0, 4.0, 8.0))

        await end(consumer, encounter.session_id, FakePubSub())

        assert slept == []

    async def test_an_encounter_with_no_coded_nudge_assembles_nothing(
        self, _stub_store: list[dict[str, Any]], caplog: Any
    ) -> None:
        """The ordinary case for most visits: nothing was flagged."""
        encounter = an_encounter()
        session = FakeSession(encounter=encounter, nudges=[], notes=[a_note()])
        consumer, slept = build(session, context=a_context(), retry_delays=(2.0,))

        with caplog.at_level(logging.INFO):
            await end(consumer, encounter.session_id, FakePubSub())

        assert _stub_store == []
        assert "nothing to request authorization for" in caplog.text
        # Ruled out before the backoff — an ordinary visit does not wait on a
        # note it has no use for.
        assert slept == []

    async def test_keyword_only_nudges_alone_assemble_nothing(
        self, _stub_store: list[dict[str, Any]]
    ) -> None:
        encounter = an_encounter()
        session = FakeSession(
            encounter=encounter, nudges=[a_nudge(cpt_code=None)], notes=[a_note()]
        )
        consumer, _ = build(session, context=a_context())

        await end(consumer, encounter.session_id, FakePubSub())

        assert _stub_store == []

    async def test_an_unreachable_chart_context_writes_no_row(
        self, _stub_store: list[dict[str, Any]]
    ) -> None:
        """Nothing was written, so a retry is clean."""
        encounter = an_encounter()
        session = FakeSession(encounter=encounter, nudges=[a_nudge()], notes=[a_note()])
        consumer, _ = build(session, context=None)

        await end(consumer, encounter.session_id, FakePubSub())

        assert _stub_store == []


class TestTheNoteRace:
    async def test_the_note_appearing_on_a_later_attempt_still_assembles(
        self, _stub_store: list[dict[str, Any]]
    ) -> None:
        """TASK-060's documented race: the SOAP pass has not finished yet."""
        encounter = an_encounter()
        session = FakeSession(encounter=encounter, nudges=[a_nudge()], notes=[None, None, a_note()])
        consumer, slept = build(session, context=a_context(), retry_delays=(2.0, 4.0, 8.0))

        await end(consumer, encounter.session_id, FakePubSub())

        assert len(_stub_store) == 1
        assert slept == [2.0, 4.0]

    async def test_the_backoff_expires_into_a_warning_not_silence(
        self, _stub_store: list[dict[str, Any]], caplog: Any
    ) -> None:
        encounter = an_encounter()
        session = FakeSession(encounter=encounter, nudges=[a_nudge()], notes=[])
        consumer, slept = build(session, context=a_context(), retry_delays=(2.0, 4.0, 8.0))

        with caplog.at_level(logging.WARNING):
            await end(consumer, encounter.session_id, FakePubSub())

        assert _stub_store == []
        assert "No clinical note for encounter" in caplog.text
        assert slept == [2.0, 4.0, 8.0]

    async def test_each_attempt_expires_the_session_snapshot(self) -> None:
        """Without the rollback the retry would re-read the same empty snapshot."""
        encounter = an_encounter()
        session = FakeSession(encounter=encounter, nudges=[a_nudge()], notes=[None, a_note()])
        consumer, _ = build(session, context=a_context(), retry_delays=(1.0,))

        await end(consumer, encounter.session_id, FakePubSub())

        assert session.rollbacks == 1

    async def test_no_delays_configured_means_one_attempt(
        self, _stub_store: list[dict[str, Any]]
    ) -> None:
        encounter = an_encounter()
        session = FakeSession(encounter=encounter, nudges=[a_nudge()], notes=[])
        consumer, slept = build(session, context=a_context(), retry_delays=())

        await end(consumer, encounter.session_id, FakePubSub())

        assert slept == []
        assert _stub_store == []


class TestDuplicateDelivery:
    async def test_a_second_end_signal_starts_no_second_assembly(
        self, _stub_store: list[dict[str, Any]]
    ) -> None:
        """The constraint stops the second row; this stops the second wait."""
        encounter = an_encounter()
        session = FakeSession(encounter=encounter, nudges=[a_nudge()], notes=[a_note(), a_note()])
        consumer, _ = build(session, context=a_context())
        pubsub = FakePubSub()
        channel = f"session:ended:{encounter.session_id}"

        # Both delivered before either assembly is awaited, which is the race.
        await consumer.handle_message(pubsub, {"channel": channel, "data": ""})
        await consumer.handle_message(pubsub, {"channel": channel, "data": ""})
        for task in list(consumer._assemblies.values()):  # noqa: SLF001
            await task

        assert len(_stub_store) == 1


class TestFailureIsReported:
    async def test_an_unexpected_error_is_logged_and_does_not_escape(
        self, caplog: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("database went away")

        monkeypatch.setattr(assembly, "load_encounter", explode)
        consumer, _ = build(FakeSession(encounter=None, nudges=[], notes=[]))

        with caplog.at_level(logging.ERROR):
            await end(consumer, uuid.uuid4(), FakePubSub())

        assert "failed" in caplog.text
        assert "no request was recorded" in caplog.text


class TestLifecycle:
    async def test_health_is_false_before_start(self) -> None:
        consumer, _ = build(None)
        assert consumer.is_healthy() is False

    async def test_stop_cancels_an_assembly_in_flight(self, caplog: Any) -> None:
        consumer, _ = build(None)
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(forever())
        consumer._assemblies[uuid.uuid4()] = task  # noqa: SLF001
        await started.wait()

        with caplog.at_level(logging.WARNING):
            await consumer.stop()

        assert task.cancelled()
        assert "in flight" in caplog.text

    async def test_stop_without_start_is_a_no_op(self) -> None:
        consumer, _ = build(None)
        await consumer.stop()

    def test_the_channel_helper_matches_the_canonical_pattern(self) -> None:
        session_id = uuid.uuid4()
        assert consumer_module.session_ended_channel(session_id) == f"session:ended:{session_id}"

    async def test_a_bytes_channel_name_is_decoded(self) -> None:
        """redis-py hands back bytes unless the client decodes responses."""
        consumer, _ = build(None)
        pubsub = FakePubSub()
        session_id = uuid.uuid4()

        await consumer.handle_message(
            pubsub,
            {
                "channel": b"sessions:started",
                "data": json.dumps({"session_id": str(session_id)}).encode(),
            },
        )

        assert consumer.watched_sessions == frozenset({session_id})

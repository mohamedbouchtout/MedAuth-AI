"""The nudge emitter: what fires, what escalates, and what is written first.

Three things are asserted here, and they are the three the task settled rather
than the three that were easy to write.

The trigger is the message and nothing else — no condition on
``missing_criteria`` or ``denial_risk`` reappears in this module, because a
second derivation of that judgement is what left two real answers unshown.

``haptic`` is not the risk level. A fallback is honestly high risk and still
must not buzz, because an outage that escalates once per procedure across every
concurrent encounter teaches a physician to ignore the escalation.

And the row is written before the publish, because the payload carries its id.
The store is covered against a real database in
``tests/integration/test_nudge_emission.py`` — the ``ON CONFLICT`` path is
PostgreSQL behaviour and a fake would only assert that the fake works.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from track_b_rag import nudges
from track_b_rag.api.schemas import PolicyQueryData
from track_b_rag.policy_dispatch import PolicyQueryParameters

SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROVIDER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ENCOUNTER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
NUDGE_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

PARAMETERS = PolicyQueryParameters(
    procedure="knee MRI",
    cpt_code="73721",
    payer="Aetna",
    plan_type="PPO",
    state="MA",
    provider_id=PROVIDER_ID,
    encounter_id=ENCOUNTER_ID,
)


def answer(**overrides: Any) -> PolicyQueryData:
    """A policy answer with something to say, overriding what a test cares about."""
    values: dict[str, Any] = {
        "requires_auth": True,
        "auth_criteria": ["Failed six weeks of conservative therapy"],
        "missing_criteria": ["Failed six weeks of conservative therapy"],
        "denial_risk": "high",
        "nudge_message": "Prior authorization required for knee MRI.",
        "step_therapy_required": False,
        "step_therapy_details": None,
        "policy_source": "L33575",
        "source": "rag",
    }
    values.update(overrides)
    return PolicyQueryData.model_validate(values)


class FakeRedis:
    """Records what was published, and where."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.published: list[tuple[str, str]] = []
        self.raises = raises

    async def publish(self, channel: str, payload: str) -> int:
        if self.raises is not None:
            raise self.raises
        self.published.append((channel, payload))
        return 1


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the store call, so these tests need no database."""
    calls: list[dict[str, Any]] = []

    async def fake_store(**kwargs: Any) -> tuple[uuid.UUID, bool]:
        calls.append(kwargs)
        return NUDGE_ID, True

    monkeypatch.setattr(nudges, "_store", fake_store)
    return calls


async def emit(redis: FakeRedis, **overrides: Any) -> uuid.UUID | None:
    return await nudges.emit(
        redis=redis,  # type: ignore[arg-type]  # only publish is exercised
        session_id=SESSION_ID,
        parameters=PARAMETERS,
        answer=answer(**overrides),
    )


def published_payload(redis: FakeRedis) -> dict[str, Any]:
    ((_, raw),) = redis.published
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


# --- the trigger -----------------------------------------------------------


async def test_an_answer_with_a_message_raises_a_nudge(stored: list[dict[str, Any]]) -> None:
    redis = FakeRedis()

    nudge_id = await emit(redis)

    assert nudge_id == NUDGE_ID
    assert len(stored) == 1
    assert published_payload(redis)["nudge_id"] == str(NUDGE_ID)


async def test_no_message_means_no_row_and_no_publish(stored: list[dict[str, Any]]) -> None:
    """The trigger, and the only one.

    ``gap_analysis`` decides whether there is anything worth interrupting for,
    and this module obeys. Note the answer here still carries a risk level and
    could still be read as "worth mentioning" by a second condition — which is
    exactly the second condition that must not exist.
    """
    redis = FakeRedis()

    nudge_id = await emit(redis, nudge_message=None, denial_risk="medium")

    assert nudge_id is None
    assert stored == []
    assert redis.published == []


async def test_a_medium_risk_answer_with_a_message_still_nudges(
    stored: list[dict[str, Any]],
) -> None:
    """The regression the trigger change exists for, seen from the emitter.

    Authorization required, no criteria found: medium risk, nothing missing,
    and a message asking the provider to confirm manually. Under the original
    trigger neither leg fired and this nudge did not exist.
    """
    redis = FakeRedis()

    nudge_id = await emit(
        redis,
        denial_risk="medium",
        auth_criteria=[],
        missing_criteria=[],
        nudge_message=(
            "Prior authorization required for knee MRI, but no published criteria "
            "were found for this plan — confirm the requirements manually."
        ),
    )

    assert nudge_id == NUDGE_ID
    assert published_payload(redis)["missing_criteria"] == []


# --- escalation ------------------------------------------------------------


async def test_a_high_risk_answer_escalates(stored: list[dict[str, Any]]) -> None:
    redis = FakeRedis()

    await emit(redis, denial_risk="high")

    assert published_payload(redis)["haptic"] is True


@pytest.mark.parametrize("risk", ["low", "medium"])
async def test_anything_short_of_high_does_not_escalate(
    stored: list[dict[str, Any]], risk: str
) -> None:
    redis = FakeRedis()

    await emit(redis, denial_risk=risk)

    assert published_payload(redis)["haptic"] is False


async def test_a_fallback_nudges_without_escalating(stored: list[dict[str, Any]]) -> None:
    """The rule this task exists to state, and the one most likely to be "fixed".

    A fallback means nothing verified the requirement, so `high` is honest and
    the provider should see it. What must not happen is the device buzzing:
    one unreachable Qdrant would escalate once per procedure in every live
    encounter, and a physician who learns the buzz means an outage stops
    reacting to the buzz that means a denial.
    """
    redis = FakeRedis()

    await emit(
        redis,
        source="fallback",
        denial_risk="high",
        auth_criteria=[],
        missing_criteria=[],
        policy_source=None,
        nudge_message="Unable to verify authorization requirements — confirm manually",
    )

    payload = published_payload(redis)
    assert payload["denial_risk"] == "high"
    assert payload["haptic"] is False


# --- the payload -----------------------------------------------------------


async def test_the_payload_matches_the_agreed_shape(stored: list[dict[str, Any]]) -> None:
    """CLAUDE.md, "The nudge payload — one shape". Four tasks read this."""
    redis = FakeRedis()

    await emit(redis)

    channel, _ = redis.published[0]
    assert channel == f"nudges:{SESSION_ID}"
    assert published_payload(redis) == {
        "type": "PAYER_RULE_ALERT",
        "nudge_id": str(NUDGE_ID),
        "procedure": "knee MRI",
        "cpt_code": "73721",
        "message": "Prior authorization required for knee MRI.",
        "missing_criteria": ["Failed six weeks of conservative therapy"],
        "denial_risk": "high",
        "haptic": True,
    }


async def test_the_payload_carries_nothing_from_the_clinical_context(
    stored: list[dict[str, Any]],
) -> None:
    """A nudge crosses a WebSocket and renders in a browser.

    The criteria are the payer's own published words and the procedure is what
    the clinician said; neither is patient detail. What must never appear is
    anything from the encounter's documentation, which the emitter is never
    given in the first place — asserted here so that stays true if someone
    later passes it in for convenience.
    """
    redis = FakeRedis()

    await emit(redis)

    assert set(published_payload(redis)) == {
        "type",
        "nudge_id",
        "procedure",
        "cpt_code",
        "message",
        "missing_criteria",
        "denial_risk",
        "haptic",
    }


# --- ordering --------------------------------------------------------------


async def test_the_row_is_written_before_the_publish(stored: list[dict[str, Any]]) -> None:
    """A client must not be able to acknowledge a nudge nobody recorded.

    Asserted through the failure direction, which is the one that matters: when
    the publish fails the row is already there, and the emitter raises so the
    consumer gives its dedup claim back and the next mention republishes it.
    """
    redis = FakeRedis(raises=RuntimeError("redis is unreachable"))

    with pytest.raises(RuntimeError):
        await emit(redis)

    assert len(stored) == 1
    assert redis.published == []


async def test_a_republished_nudge_is_logged_as_such(
    stored: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The retry path says which it did, so a duplicate is visible if one appears."""

    async def existing(**_: Any) -> tuple[uuid.UUID, bool]:
        return NUDGE_ID, False

    monkeypatch.setattr(nudges, "_store", existing)
    redis = FakeRedis()

    with caplog.at_level("INFO", logger="track_b_rag.nudges"):
        nudge_id = await emit(redis)

    assert nudge_id == NUDGE_ID
    assert "republished" in caplog.text
    assert published_payload(redis)["nudge_id"] == str(NUDGE_ID)


async def test_the_log_line_carries_no_criteria_or_message(
    stored: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    """The row holds all of it; stdout does not need the clinical detail."""
    redis = FakeRedis()

    with caplog.at_level("INFO", logger="track_b_rag.nudges"):
        await emit(redis)

    assert "conservative therapy" not in caplog.text
    assert "Prior authorization required" not in caplog.text

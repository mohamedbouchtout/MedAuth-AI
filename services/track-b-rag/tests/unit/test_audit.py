"""The audit writes for this service's routes: what they record, and what they must not.

The row is the compliance artefact for a PHI access, so the assertions here are
about its contents rather than about the fact that a call happened: identifiers
only, no clinical detail, and no procedure or code — hipaa-logger records *that*
an access occurred, never what was in it.

The write itself, against the real table and its constraints, is covered in
``tests/integration/test_policy_query.py``.

``audit_nudge_acknowledge`` (TASK-041b) is here too, and the assertion that
matters for it is which of two actions it writes. A repeat dismissal reads a row
it does not move, so recording it as a state change would make the trail claim
something that did not happen — the same call track-a-clinical's idempotent
session-end makes when it audits as ``READ_ENCOUNTER``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from track_b_rag import audit

SESSION_ID = uuid.UUID("3f2504e0-4f89-41d3-9a0c-0305e82c3301")
PROVIDER_ID = uuid.UUID("9c858901-8a57-4791-81fe-4c455b099bc9")


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what would have been written, without a database in reach."""
    calls: list[dict[str, Any]] = []

    async def fake_audit_log(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(audit, "audit_log", fake_audit_log)
    return calls


async def test_the_row_names_the_actor_and_the_session(
    recorded: list[dict[str, Any]],
) -> None:
    await audit.audit_policy_query(session_id=SESSION_ID, provider_id=PROVIDER_ID)

    row = recorded[0]
    assert row["actor_id"] == str(PROVIDER_ID)
    assert row["session_id"] == str(SESSION_ID)
    assert row["resource_id"] == str(SESSION_ID)


async def test_the_row_uses_the_shared_vocabulary(recorded: list[dict[str, Any]]) -> None:
    """The action and resource type come from CLAUDE.md's schema comment, not free text."""
    await audit.audit_policy_query(session_id=SESSION_ID, provider_id=PROVIDER_ID)

    row = recorded[0]
    assert row["action"] == "QUERY_POLICY"
    assert row["resource_type"] == "Encounter"
    assert row["service_name"] == "track-b-rag"


async def test_the_request_metadata_is_passed_through(recorded: list[dict[str, Any]]) -> None:
    await audit.audit_policy_query(
        session_id=SESSION_ID,
        provider_id=PROVIDER_ID,
        ip_address="10.1.2.3",
        user_agent="track-b-rag-consumer/1.0",
    )

    row = recorded[0]
    assert row["ip_address"] == "10.1.2.3"
    assert row["user_agent"] == "track-b-rag-consumer/1.0"


async def test_the_write_goes_through_the_shared_pool(recorded: list[dict[str, Any]]) -> None:
    """No connection is injected: this route writes nothing of its own to join.

    track-a-clinical passes its request's connection so the audit row and the
    encounter row commit together. There is no such row here.
    """
    await audit.audit_policy_query(session_id=SESSION_ID, provider_id=PROVIDER_ID)

    assert "conn" not in recorded[0]


async def test_a_failed_audit_write_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """HIPAA requires a durable record, so an unrecordable access has to fail."""

    async def failing(**kwargs: Any) -> None:
        raise RuntimeError("audit_log is down")

    monkeypatch.setattr(audit, "audit_log", failing)

    with pytest.raises(RuntimeError, match="audit_log is down"):
        await audit.audit_policy_query(session_id=SESSION_ID, provider_id=PROVIDER_ID)


NUDGE_ID = uuid.UUID("0b7f6c9a-2f4d-4a1e-9d3b-6d0f1c5a8e42")


async def test_an_acknowledgement_that_changed_the_row_is_a_state_change(
    recorded: list[dict[str, Any]],
) -> None:
    await audit.audit_nudge_acknowledge(
        nudge_id=NUDGE_ID,
        session_id=SESSION_ID,
        provider_id=PROVIDER_ID,
        changed=True,
        conn=object(),  # type: ignore[arg-type]
    )

    row = recorded[0]
    assert row["action"] == "ACKNOWLEDGE_NUDGE"
    assert row["resource_type"] == "ClinicalNudge"
    assert row["resource_id"] == str(NUDGE_ID)
    assert row["session_id"] == str(SESSION_ID)


async def test_a_repeat_acknowledgement_is_recorded_as_a_read(
    recorded: list[dict[str, Any]],
) -> None:
    """The row was read, not changed — TASK-006's precedent, one table over."""
    await audit.audit_nudge_acknowledge(
        nudge_id=NUDGE_ID,
        session_id=SESSION_ID,
        provider_id=PROVIDER_ID,
        changed=False,
        conn=object(),  # type: ignore[arg-type]
    )

    assert recorded[0]["action"] == "READ_NUDGE"


async def test_the_acknowledgement_actor_is_the_encounters_provider(
    recorded: list[dict[str, Any]],
) -> None:
    """This route carries no credential, so the encounter is the only actor there is."""
    await audit.audit_nudge_acknowledge(
        nudge_id=NUDGE_ID,
        session_id=SESSION_ID,
        provider_id=PROVIDER_ID,
        changed=True,
        conn=object(),  # type: ignore[arg-type]
    )

    assert recorded[0]["actor_id"] == str(PROVIDER_ID)


async def test_the_acknowledgement_joins_the_routes_transaction(
    recorded: list[dict[str, Any]],
) -> None:
    """Unlike the policy query, this route writes a row for the audit to commit with."""
    connection = object()

    await audit.audit_nudge_acknowledge(
        nudge_id=NUDGE_ID,
        session_id=SESSION_ID,
        provider_id=PROVIDER_ID,
        changed=True,
        conn=connection,  # type: ignore[arg-type]
    )

    assert recorded[0]["conn"] is connection


async def test_the_acknowledgement_carries_the_client_it_came_from(
    recorded: list[dict[str, Any]],
) -> None:
    """A browser is the caller here, unlike the consumer-triggered nudge write."""
    await audit.audit_nudge_acknowledge(
        nudge_id=NUDGE_ID,
        session_id=SESSION_ID,
        provider_id=PROVIDER_ID,
        changed=True,
        conn=object(),  # type: ignore[arg-type]
        ip_address="10.1.2.3",
        user_agent="Mozilla/5.0",
    )

    row = recorded[0]
    assert row["ip_address"] == "10.1.2.3"
    assert row["user_agent"] == "Mozilla/5.0"

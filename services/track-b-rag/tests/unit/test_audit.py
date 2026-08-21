"""The audit write for POST /policies/query: what it records, and what it must not.

The row is the compliance artefact for a PHI access, so the assertions here are
about its contents rather than about the fact that a call happened: identifiers
only, no clinical detail, and no procedure or code — hipaa-logger records *that*
an access occurred, never what was in it.

The write itself, against the real table and its constraints, is covered in
``tests/integration/test_policy_query.py``.
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

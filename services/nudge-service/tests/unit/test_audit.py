"""The audit row this service writes, and the fields it puts in it.

The rule these tests encode is Known Constraints #6: a route audits if and only
if it touches PHI. The route tests cover the "if not" half — a refused connection
writes nothing. This covers the "if" half, and the shape of the row.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from src import audit


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the call to hipaa-logger instead of writing a row."""
    calls: list[dict[str, Any]] = []

    async def fake_audit_log(**fields: Any) -> None:
        calls.append(fields)

    monkeypatch.setattr(audit, "audit_log", fake_audit_log)
    return calls


async def test_the_row_identifies_the_provider_and_the_encounter(
    recorded: list[dict[str, Any]],
) -> None:
    session_id = uuid.uuid4()
    provider_id = uuid.uuid4()

    await audit.audit_nudge_stream(session_id=session_id, provider_id=provider_id)

    assert recorded[0]["actor_id"] == str(provider_id)
    assert recorded[0]["session_id"] == str(session_id)
    assert recorded[0]["resource_id"] == str(session_id)


async def test_the_resource_is_the_encounter_not_a_nudge_row(
    recorded: list[dict[str, Any]],
) -> None:
    """The access is to one encounter's stream, not to a particular nudge.

    This service never reads ``clinical_nudges`` — track-b-rag writes it and
    audits that separately as ``WRITE_NUDGE``.
    """
    await audit.audit_nudge_stream(session_id=uuid.uuid4(), provider_id=uuid.uuid4())

    assert recorded[0]["resource_type"] == "Encounter"


async def test_the_action_names_what_actually_happened(
    recorded: list[dict[str, Any]],
) -> None:
    await audit.audit_nudge_stream(session_id=uuid.uuid4(), provider_id=uuid.uuid4())

    assert recorded[0]["action"] == audit.ACTION_RELAY_NUDGES == "RELAY_NUDGES"


async def test_the_row_names_this_service(recorded: list[dict[str, Any]]) -> None:
    """Every service writes to one table; the row has to say which one wrote it."""
    await audit.audit_nudge_stream(session_id=uuid.uuid4(), provider_id=uuid.uuid4())

    assert recorded[0]["service_name"] == "nudge-service"


async def test_the_client_address_and_agent_are_recorded_when_known(
    recorded: list[dict[str, Any]],
) -> None:
    await audit.audit_nudge_stream(
        session_id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        ip_address="10.1.2.3",
        user_agent="MedAuth/1.0 (iOS)",
    )

    assert recorded[0]["ip_address"] == "10.1.2.3"
    assert recorded[0]["user_agent"] == "MedAuth/1.0 (iOS)"


async def test_no_nudge_content_can_reach_the_row(
    recorded: list[dict[str, Any]],
) -> None:
    """The audit trail records that PHI was accessed, never the PHI itself.

    A nudge names a procedure and the criteria a patient's documentation is
    missing; none of that belongs in a compliance row about who connected.
    """
    await audit.audit_nudge_stream(session_id=uuid.uuid4(), provider_id=uuid.uuid4())

    assert set(recorded[0]) == {
        "actor_id",
        "action",
        "resource_type",
        "resource_id",
        "session_id",
        "service_name",
        "ip_address",
        "user_agent",
    }


async def test_the_write_uses_hipaa_loggers_own_pool(
    recorded: list[dict[str, Any]],
) -> None:
    """No ``conn`` is passed, unlike track-a-clinical.

    That service joins the audit write to its own request transaction. This one
    owns no tables and holds no database session, so there is no transaction to
    join and the package's pool is the right place for the write.
    """
    await audit.audit_nudge_stream(session_id=uuid.uuid4(), provider_id=uuid.uuid4())

    assert "conn" not in recorded[0]

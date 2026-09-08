"""The audit row an assembly writes.

The row is the only record that this encounter's clinical content was read and a
new PHI record made from it — there is no request behind the work, so nothing
else records the access.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from hipaa_logger import AuditAction
from prior_auth import audit


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what would have been written, and hand back the assembly's connection."""
    calls: list[dict[str, Any]] = []

    async def fake_audit_log(**kwargs: Any) -> None:
        calls.append(kwargs)

    async def fake_connection(session: Any) -> str:
        return f"raw-connection-of-{session}"

    monkeypatch.setattr(audit, "audit_log", fake_audit_log)
    monkeypatch.setattr(audit, "raw_asyncpg_connection", fake_connection)
    return calls


class TestTheBundleWrite:
    async def test_it_records_the_action_the_resource_and_the_actor(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        request_id, session_id, provider_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        await audit.audit_bundle_write(
            "session",  # type: ignore[arg-type]
            request_id=request_id,
            session_id=session_id,
            provider_id=provider_id,
        )

        assert recorded == [
            {
                "actor_id": str(provider_id),
                "action": AuditAction.WRITE_PRIOR_AUTH,
                "resource_type": "PriorAuthRequest",
                "resource_id": str(request_id),
                "session_id": str(session_id),
                "service_name": "prior-auth",
                "conn": "raw-connection-of-session",
            }
        ]

    async def test_the_action_comes_from_the_shared_vocabulary(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        """Never a local string constant — that is how the vocabulary drifted thrice."""
        await audit.audit_bundle_write(
            "s",  # type: ignore[arg-type]
            request_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            provider_id=uuid.uuid4(),
        )
        assert isinstance(recorded[0]["action"], AuditAction)

    async def test_a_missing_provider_is_an_honest_null(
        self, recorded: list[dict[str, Any]]
    ) -> None:
        """Never a service account invented to fill the field.

        A fabricated identifier in an audit trail is worse than an honest null.
        """
        await audit.audit_bundle_write(
            "s",  # type: ignore[arg-type]
            request_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            provider_id=None,
        )
        assert recorded[0]["actor_id"] is None

    async def test_no_client_fields_are_sent(self, recorded: list[dict[str, Any]]) -> None:
        """Permanently absent here rather than waiting on request-context middleware.

        This write has no client and never will — it is driven by a Redis
        signal.
        """
        await audit.audit_bundle_write(
            "s",  # type: ignore[arg-type]
            request_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            provider_id=uuid.uuid4(),
        )
        assert "ip_address" not in recorded[0]
        assert "user_agent" not in recorded[0]

    async def test_it_joins_the_callers_transaction(self, recorded: list[dict[str, Any]]) -> None:
        """A bundle with no audit row, or an audit row for a rolled-back bundle,
        are both worse than the write failing outright."""
        await audit.audit_bundle_write(
            "the-assembly-session",  # type: ignore[arg-type]
            request_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            provider_id=uuid.uuid4(),
        )
        assert recorded[0]["conn"] == "raw-connection-of-the-assembly-session"

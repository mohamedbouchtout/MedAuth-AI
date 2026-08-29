"""PATCH /nudges/{nudge_id}/acknowledge — the HTTP contract and the lookup it does.

The database is faked here so the envelope, the status codes, the idempotent
repeat and the body validation stay testable without PostgreSQL. Two things a
fake genuinely cannot prove are left to
``tests/integration/test_nudge_acknowledge.py``: that the row actually changes,
and that the join to ``encounters`` hides a soft-deleted encounter's nudges — a
fake ``execute`` returns whatever the test handed it, whatever the statement
said. What *is* checked here is that the statement says the right thing, because
a route that dropped the join would otherwise pass every unit test in this file.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from track_a_clinical.models import ClinicalNudge
from track_b_rag import db
from track_b_rag.api import nudges
from track_b_rag.api.dependencies import get_db_session
from track_b_rag.main import create_app

NUDGE_ID = uuid.UUID("0b7f6c9a-2f4d-4a1e-9d3b-6d0f1c5a8e42")
SESSION_ID = uuid.UUID("3f2504e0-4f89-41d3-9a0c-0305e82c3301")
PROVIDER_ID = uuid.UUID("9c858901-8a57-4791-81fe-4c455b099bc9")

FIRED_AT = datetime.datetime(2026, 8, 28, 14, 30, tzinfo=datetime.UTC)
ACKNOWLEDGED_AT = datetime.datetime(2026, 8, 28, 14, 31, tzinfo=datetime.UTC)


def nudge(*, acknowledged: bool = False) -> ClinicalNudge:
    """A nudge row as the select would return it, unattached to any session."""
    row = ClinicalNudge(
        encounter_id=uuid.uuid4(),
        procedure_name="knee MRI",
        cpt_code="73721",
        nudge_message="Prior authorization required for knee MRI.",
        missing_criteria=["Failed six weeks of conservative therapy"],
        denial_risk="high",
    )
    row.id = NUDGE_ID
    row.fired_at = FIRED_AT
    row.acknowledged = acknowledged
    row.acknowledged_at = ACKNOWLEDGED_AT if acknowledged else None
    return row


class FakeResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def one_or_none(self) -> tuple[Any, ...] | None:
        return self._row


class FakeSession:
    """Answers one select, counts the commit, and stands in for the read-back.

    ``refresh`` writes a real datetime because the route sets ``acknowledged_at``
    to ``sa.func.now()`` — a SQL construct, not a value — and it is the refresh
    that turns it into something a response model can carry. A fake that did
    nothing there would make the route look broken for a reason the database
    does not have.
    """

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row
        self.statements: list[Any] = []
        self.commits = 0
        self.refreshed: list[Any] = []

    async def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self._row)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, instance: Any) -> None:
        self.refreshed.append(instance)
        instance.acknowledged_at = ACKNOWLEDGED_AT


class AuditRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture(autouse=True)
def audited(monkeypatch: pytest.MonkeyPatch) -> AuditRecorder:
    """Every request through this module records its audit call instead of writing it."""
    recorder = AuditRecorder()
    monkeypatch.setattr(nudges.audit, "audit_nudge_acknowledge", recorder)
    return recorder


@pytest.fixture(autouse=True)
def no_raw_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake session has no asyncpg connection under it to hand out."""

    async def fake_connection(session: Any) -> object:
        return object()

    monkeypatch.setattr(db, "raw_asyncpg_connection", fake_connection)


def build_client(session: FakeSession) -> AsyncClient:
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: session
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://track-b-rag")


@pytest_asyncio.fixture
async def unacknowledged() -> AsyncIterator[tuple[AsyncClient, FakeSession]]:
    session = FakeSession((nudge(), SESSION_ID, PROVIDER_ID))
    async with build_client(session) as http:
        yield http, session


@pytest_asyncio.fixture
async def acknowledged() -> AsyncIterator[tuple[AsyncClient, FakeSession]]:
    session = FakeSession((nudge(acknowledged=True), SESSION_ID, PROVIDER_ID))
    async with build_client(session) as http:
        yield http, session


@pytest_asyncio.fixture
async def missing() -> AsyncIterator[AsyncClient]:
    async with build_client(FakeSession(None)) as http:
        yield http


# --- the success contract --------------------------------------------------


async def test_an_acknowledgement_returns_the_standard_envelope(
    unacknowledged: tuple[AsyncClient, FakeSession],
) -> None:
    http, _ = unacknowledged

    response = await http.patch(f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": True})

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["data"] == {
        "nudge_id": str(NUDGE_ID),
        "acknowledged": True,
        "acknowledged_at": ACKNOWLEDGED_AT.isoformat().replace("+00:00", "Z"),
        "already_acknowledged": False,
    }


async def test_the_row_is_marked_and_committed(
    unacknowledged: tuple[AsyncClient, FakeSession],
) -> None:
    http, session = unacknowledged

    await http.patch(f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": True})

    assert session.commits == 1
    assert session.refreshed, "acknowledged_at is a SQL construct until it is read back"


async def test_the_audit_names_the_encounters_provider(
    unacknowledged: tuple[AsyncClient, FakeSession], audited: AuditRecorder
) -> None:
    """No credential is presented, so the encounter is the only source of an actor."""
    http, _ = unacknowledged

    await http.patch(f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": True})

    (call,) = audited.calls
    assert call["provider_id"] == PROVIDER_ID
    assert call["session_id"] == SESSION_ID
    assert call["nudge_id"] == NUDGE_ID
    assert call["changed"] is True


async def test_the_audit_carries_the_client_the_request_came_from(
    unacknowledged: tuple[AsyncClient, FakeSession], audited: AuditRecorder
) -> None:
    http, _ = unacknowledged

    await http.patch(
        f"/nudges/{NUDGE_ID}/acknowledge",
        json={"acknowledged": True},
        headers={"user-agent": "medauth-web/0.1"},
    )

    (call,) = audited.calls
    assert call["user_agent"] == "medauth-web/0.1"
    assert call["ip_address"] == "127.0.0.1"


# --- the idempotent repeat, copied from TASK-006's session end ---------------


async def test_a_repeat_acknowledgement_keeps_the_original_timestamp(
    acknowledged: tuple[AsyncClient, FakeSession],
) -> None:
    """A double tap must not move a timestamp recording when a provider saw the alert."""
    http, session = acknowledged

    response = await http.patch(f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": True})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["already_acknowledged"] is True
    assert body["acknowledged_at"] == ACKNOWLEDGED_AT.isoformat().replace("+00:00", "Z")
    assert session.refreshed == [], "nothing changed, so there is nothing to read back"


async def test_a_repeat_acknowledgement_audits_as_a_read(
    acknowledged: tuple[AsyncClient, FakeSession], audited: AuditRecorder
) -> None:
    """The row was read, not changed — the distinction TASK-006 already makes."""
    http, _ = acknowledged

    await http.patch(f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": True})

    assert audited.calls[0]["changed"] is False


async def test_a_nudge_acknowledged_outside_this_route_still_answers(
    unacknowledged: tuple[AsyncClient, FakeSession],
) -> None:
    """``fired_at`` stands in when the flag is set but the timestamp is not.

    Nothing in this repository writes that state, which is exactly why the
    response model must not depend on it never occurring.
    """
    session = FakeSession((nudge(), SESSION_ID, PROVIDER_ID))
    row, *_ = session._row or ()
    row.acknowledged = True
    row.acknowledged_at = None

    async with build_client(session) as http:
        response = await http.patch(f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": True})

    assert response.json()["data"]["acknowledged_at"] == FIRED_AT.isoformat().replace("+00:00", "Z")


# --- what is refused --------------------------------------------------------


async def test_an_unknown_nudge_is_a_404(missing: AsyncClient) -> None:
    response = await missing.patch(f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": True})

    assert response.status_code == 404
    body = response.json()
    assert body["data"] is None
    assert body["error"]["code"] == "nudge_not_found"


async def test_an_unknown_nudge_writes_no_audit_row(
    missing: AsyncClient, audited: AuditRecorder
) -> None:
    """Nothing was reached, so nothing is recorded as reached."""
    await missing.patch(f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": True})

    assert audited.calls == []


async def test_false_is_rejected_rather_than_un_acknowledging(
    unacknowledged: tuple[AsyncClient, FakeSession],
) -> None:
    """The reverse transition is not specified, and inventing it here would put a
    compliance-relevant flag under a caller's control in both directions."""
    http, session = unacknowledged

    response = await http.patch(f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": False})

    assert response.status_code == 422
    assert session.commits == 0


async def test_an_absent_body_is_rejected(
    unacknowledged: tuple[AsyncClient, FakeSession],
) -> None:
    """The transition is stated, not implied — that is the point of the field."""
    http, _ = unacknowledged

    response = await http.patch(f"/nudges/{NUDGE_ID}/acknowledge")

    assert response.status_code == 422


async def test_an_unexpected_field_is_rejected(
    unacknowledged: tuple[AsyncClient, FakeSession],
) -> None:
    """`extra="forbid"`, so a client sending a field this route does not have
    hears about it rather than watching it be ignored."""
    http, _ = unacknowledged

    response = await http.patch(
        f"/nudges/{NUDGE_ID}/acknowledge",
        json={"acknowledged": True, "reason": "dismissed"},
    )

    assert response.status_code == 422


async def test_a_nudge_id_that_is_not_a_uuid_is_rejected(
    unacknowledged: tuple[AsyncClient, FakeSession],
) -> None:
    http, _ = unacknowledged

    response = await http.patch("/nudges/not-a-uuid/acknowledge", json={"acknowledged": True})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_a_rejected_body_never_reaches_the_error_message(
    unacknowledged: tuple[AsyncClient, FakeSession],
) -> None:
    """api-envelope's handler reports field locations, never what was sent."""
    http, _ = unacknowledged

    response = await http.patch(
        f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": "dismissed by dr smith"}
    )

    assert response.status_code == 422
    assert "dr smith" not in response.text


# --- the lookup itself ------------------------------------------------------


async def test_the_lookup_joins_encounters_and_excludes_soft_deleted(
    unacknowledged: tuple[AsyncClient, FakeSession],
) -> None:
    """The join is the only thing that can see a soft-deleted encounter.

    ``clinical_nudges`` has no ``deleted_at`` of its own, so a route selecting
    the nudge row alone would leave a retired encounter's nudges dismissible and
    would pass every other test in this file. Asserted on the compiled statement
    because a fake session executes nothing;
    ``tests/integration/test_nudge_acknowledge.py`` proves the behaviour.
    """
    http, session = unacknowledged

    await http.patch(f"/nudges/{NUDGE_ID}/acknowledge", json={"acknowledged": True})

    compiled = str(session.statements[0]).lower()
    assert "join encounters" in compiled
    assert "encounters.deleted_at is null" in compiled

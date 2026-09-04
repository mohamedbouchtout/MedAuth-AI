"""``POST /providers/resolve`` — the registry that turns a practitioner into a UUID.

The fake session here stands in for PostgreSQL's ``ON CONFLICT DO NOTHING``
behaviour: an insert of a reference already present returns no row, which is what
sends the handler to the select. What only the integration suite can prove is
that the real constraint behaves that way; these tests cover the contract and the
rules that are not about the database at all — that the route audits nothing and
never logs the reference.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from track_a_clinical.api.dependencies import get_db_session
from track_a_clinical.main import create_app

PRACTITIONER_REF = "https://ehr.example.com/fhir/Practitioner/abc-123"
OTHER_REF = "https://ehr.example.com/fhir/Practitioner/xyz-789"


class FakeResult:
    """The one scalar accessor each statement in the handler reads."""

    def __init__(self, value: uuid.UUID | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> uuid.UUID | None:
        return self._value

    def scalar_one(self) -> uuid.UUID:
        assert self._value is not None
        return self._value


class FakeSession:
    """A dict standing in for the providers table, with the unique constraint."""

    def __init__(self) -> None:
        self.rows: dict[str, uuid.UUID] = {}
        self.commits = 0
        self.inserts = 0

    async def execute(self, statement: Any) -> FakeResult:
        if isinstance(statement, sa.sql.dml.Insert):
            self.inserts += 1
            reference = statement.compile().params["fhir_practitioner_ref"]
            if reference in self.rows:
                # What ON CONFLICT DO NOTHING ... RETURNING does: no row.
                return FakeResult(None)
            self.rows[reference] = uuid.uuid4()
            return FakeResult(self.rows[reference])

        reference = statement.compile().params["fhir_practitioner_ref_1"]
        return FakeResult(self.rows.get(reference))

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:  # pragma: no cover - the safety net only
        pass


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()


@pytest_asyncio.fixture
async def client(fake_session: FakeSession) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: fake_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://track-a-clinical") as http:
        yield http


async def resolve(client: AsyncClient, reference: str) -> Any:
    return await client.post("/providers/resolve", json={"fhir_practitioner_ref": reference})


@pytest.mark.asyncio
async def test_it_registers_a_practitioner_never_seen_before(
    client: AsyncClient, fake_session: FakeSession
) -> None:
    response = await resolve(client, PRACTITIONER_REF)

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    # Parses as a UUID, which is the whole point: encounters.provider_id is one.
    uuid.UUID(body["data"]["provider_id"])
    assert fake_session.commits == 1


@pytest.mark.asyncio
async def test_it_returns_the_same_provider_for_a_repeated_practitioner(
    client: AsyncClient,
) -> None:
    """One clinician is one provider forever.

    A second row for the same person would split their encounters between two
    provider ids with nothing erroring anywhere — the failure the unique
    constraint exists to make impossible.
    """
    first = await resolve(client, PRACTITIONER_REF)
    second = await resolve(client, PRACTITIONER_REF)

    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["provider_id"] == second.json()["data"]["provider_id"]


@pytest.mark.asyncio
async def test_it_keeps_two_practitioners_apart(client: AsyncClient) -> None:
    first = await resolve(client, PRACTITIONER_REF)
    second = await resolve(client, OTHER_REF)

    assert first.json()["data"]["provider_id"] != second.json()["data"]["provider_id"]


@pytest.mark.asyncio
async def test_a_repeat_resolution_answers_200_and_not_201(client: AsyncClient) -> None:
    """The status does not depend on whether the row happened to be new.

    A client cannot use "was this clinician's first launch" for anything, and a
    201-then-200 sequence would make the route behave differently for a new hire
    than for everyone else.
    """
    assert (await resolve(client, PRACTITIONER_REF)).status_code == 200
    assert (await resolve(client, PRACTITIONER_REF)).status_code == 200


@pytest.mark.asyncio
async def test_it_writes_no_audit_row(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A practitioner reference is provider identity, not PHI.

    Known Constraints #6 is an if-and-only-if in both directions: an operational
    write mixed into audit_log turns "who accessed patient X" from a query you
    can run into one you have to filter.
    """
    calls: list[dict[str, Any]] = []

    async def recorder(**fields: Any) -> None:
        calls.append(fields)

    monkeypatch.setattr("hipaa_logger.audit_log", recorder)

    await resolve(client, PRACTITIONER_REF)

    assert calls == []


@pytest.mark.asyncio
async def test_it_never_logs_the_practitioner_reference(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The reference names an individual clinician.

    An operational log is not the place to accumulate a roster of who used the
    product and when — the same rule that keeps a launch_id out of log lines.
    """
    with caplog.at_level(logging.DEBUG):
        await resolve(client, PRACTITIONER_REF)

    assert PRACTITIONER_REF not in caplog.text
    assert "abc-123" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"fhir_practitioner_ref": ""},
        {"fhir_practitioner_ref": "x" * 513},
        {"fhir_practitioner_ref": PRACTITIONER_REF, "provider_id": str(uuid.uuid4())},
    ],
    ids=["missing", "empty", "too-long", "extra-field"],
)
async def test_it_refuses_a_body_it_cannot_resolve(
    client: AsyncClient, body: dict[str, Any]
) -> None:
    """Including a caller-supplied provider_id, which the route never accepts.

    The identifier is minted here or read from here; a caller that could name it
    would be asserting a provider identity of its own.
    """
    response = await client.post("/providers/resolve", json=body)

    assert response.status_code == 422
    assert response.json()["data"] is None

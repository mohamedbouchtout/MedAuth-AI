"""Fakes that let the session routes be exercised without PostgreSQL or Redis.

The integration suite covers the same routes against real backing services; these
fakes exist so the request/response contract — envelope shape, status codes, the
idempotency rule — stays testable on a machine with neither running.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from track_a_clinical import audit
from track_a_clinical.api.dependencies import get_db_session, get_redis
from track_a_clinical.config import get_settings
from track_a_clinical.main import create_app
from track_a_clinical.models import Encounter

SIGNING_KEY = "route-test-signing-key-padded-32b"


class FakeSession:
    """Just enough AsyncSession for these two handlers.

    ``flush`` assigns the server-generated primary key the real database would
    return, because the audit write needs it before the transaction commits.
    """

    def __init__(self, existing: Encounter | None = None) -> None:
        self.existing = existing
        self.added: list[Encounter] = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshes = 0

    def add(self, instance: Encounter) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        for instance in self.added:
            if instance.id is None:
                instance.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, instance: Encounter) -> None:
        self.refreshes += 1
        # Stands in for reading back the NOW() the database wrote.
        instance.ended_at = datetime.datetime(2026, 8, 18, 12, 30, tzinfo=datetime.UTC)

    async def scalar(self, _statement: Any) -> Encounter | None:
        return self.existing


class FakeRedis:
    """Records published channels, or fails on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self.fail = fail

    async def publish(self, channel: str, message: str) -> int:
        if self.fail:
            raise RedisError("broker unreachable")
        self.published.append((channel, message))
        return 1


class RecordedAudit:
    """Captures the audit calls the handlers make instead of writing rows."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _session: Any, **fields: Any) -> None:
        self.calls.append(fields)

    @property
    def actions(self) -> list[str]:
        return [call["action"] for call in self.calls]


def make_encounter(
    *,
    session_id: uuid.UUID,
    status: str,
    ended_at: datetime.datetime | None = None,
) -> Encounter:
    """Build a detached Encounter standing in for a row already in the table."""
    encounter = Encounter(
        session_id=session_id,
        patient_fhir_id="synthea-placeholder-1",
        provider_id=uuid.uuid4(),
        status=status,
        ended_at=ended_at,
    )
    encounter.id = uuid.uuid4()
    encounter.started_at = datetime.datetime(2026, 8, 18, 12, 0, tzinfo=datetime.UTC)
    return encounter


@pytest.fixture
def signing_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)
    yield SIGNING_KEY
    get_settings.cache_clear()


@pytest.fixture
def recorded_audit(monkeypatch: pytest.MonkeyPatch) -> RecordedAudit:
    recorder = RecordedAudit()
    monkeypatch.setattr(audit, "audit_encounter_access", recorder)
    return recorder


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest_asyncio.fixture
async def client(
    fake_session: FakeSession,
    fake_redis: FakeRedis,
    signing_key: str,
    recorded_audit: RecordedAudit,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app with both backing stores replaced."""
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: fake_session
    app.dependency_overrides[get_redis] = lambda: fake_redis
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://track-a-clinical") as http:
        yield http

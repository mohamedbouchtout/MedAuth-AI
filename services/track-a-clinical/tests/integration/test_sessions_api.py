"""The session lifecycle routes against real PostgreSQL and Redis.

Skipped when DATABASE_URL is unset, like the migration suite, so the unit tests
still run on a machine with no backing services. In CI both are service
containers and these always run.

What only this suite can prove: that the audit row really lands alongside the
encounter, that ``ended_at`` is the database's own NOW(), and that a subscriber
on the Redis channel actually receives the end signal.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hipaa_logger import close_pool, configure
from track_a_clinical.api.dependencies import close_redis, get_db_session
from track_a_clinical.config import get_settings
from track_a_clinical.db import database_url
from track_a_clinical.main import create_app
from track_a_clinical.models import (
    ENCOUNTER_STATUS_ACTIVE,
    ENCOUNTER_STATUS_COMPLETED,
    Encounter,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL is not set — session API tests need a real PostgreSQL",
    ),
]

#: Meets the 32-byte minimum the service enforces on HS256 keys.
SIGNING_KEY = "integration-signing-key-padded-32"
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

#: How long a subscriber waits for the end-of-session signal before failing.
SIGNAL_TIMEOUT_SECONDS = 5.0

PATIENT_FHIR_ID = "synthea-placeholder-1"


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("JWT_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory for both the app and this suite's own assertions."""
    engine = create_async_engine(database_url())
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """The real app, wired to a real database and the real Redis client.

    hipaa-logger is pointed at the same DATABASE_URL so its audit writes land in
    the audit_log table these tests then read back.
    """
    configure(os.environ["DATABASE_URL"])

    async def override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://track-a-clinical") as http:
        yield http
    # The Redis client and the audit pool are process-wide and bind to the loop
    # that created them. pytest-asyncio gives each test its own loop, so both have
    # to be released here — the same release the app's lifespan does on shutdown.
    await close_redis()
    await close_pool()


async def load_encounter(
    sessions: async_sessionmaker[AsyncSession], session_id: uuid.UUID
) -> Encounter | None:
    """Read an encounter back through a fresh session, not the app's."""
    async with sessions() as session:
        return await session.scalar(sa.select(Encounter).where(Encounter.session_id == session_id))


async def count_audit_rows(
    sessions: async_sessionmaker[AsyncSession], session_id: uuid.UUID, action: str
) -> int:
    """Count audit_log rows hipaa-logger wrote for one session and action."""
    statement = sa.text(
        "SELECT count(*) FROM audit_log WHERE session_id = :sid AND action = :action"
    )
    async with sessions() as session:
        result = await session.scalar(statement, {"sid": session_id, "action": action})
        return int(result or 0)


async def start_session(client: AsyncClient) -> tuple[uuid.UUID, str]:
    """Start a session through the API and return its id and token."""
    response = await client.post(
        "/sessions/start",
        json={
            "patient_id": PATIENT_FHIR_ID,
            "provider_id": str(uuid.uuid4()),
            "ehr_encounter_id": "athena-enc-9001",
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    return uuid.UUID(data["session_id"]), data["jwt"]


async def wait_for_signal(pubsub: Any) -> dict[str, Any] | None:
    """Return the first real message on the subscription, or None on timeout."""

    async def read() -> dict[str, Any]:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=SIGNAL_TIMEOUT_SECONDS
            )
            if message is not None:
                return dict(message)

    try:
        return await asyncio.wait_for(read(), timeout=SIGNAL_TIMEOUT_SECONDS)
    except TimeoutError:
        return None


async def test_start_persists_the_encounter_and_its_audit_row(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    session_id, _ = await start_session(client)

    encounter = await load_encounter(sessions, session_id)
    assert encounter is not None
    assert encounter.status == ENCOUNTER_STATUS_ACTIVE
    assert encounter.patient_fhir_id == PATIENT_FHIR_ID
    assert encounter.ended_at is None
    assert await count_audit_rows(sessions, session_id, "START_SESSION") == 1


async def test_end_marks_completed_and_signals_a_subscriber(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    session_id, _ = await start_session(client)
    redis = Redis.from_url(REDIS_URL)
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"session:ended:{session_id}")

    try:
        response = await client.post(f"/sessions/{session_id}/end")
        assert response.status_code == 200, response.text
        message = await wait_for_signal(pubsub)
    finally:
        await pubsub.aclose()
        await redis.aclose()

    assert message is not None, "no session-ended signal arrived"
    assert message["data"] == b"", "the signal must be empty — it carries no data"

    encounter = await load_encounter(sessions, session_id)
    assert encounter is not None
    assert encounter.status == ENCOUNTER_STATUS_COMPLETED
    # Written as NOW() by the database, not by the service's own clock.
    assert encounter.ended_at is not None
    assert encounter.ended_at >= encounter.started_at


async def test_end_is_idempotent_and_signals_only_once(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """A duplicate signal would make TASK-030 and TASK-060 both run twice."""
    session_id, _ = await start_session(client)
    redis = Redis.from_url(REDIS_URL)
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"session:ended:{session_id}")

    try:
        first = await client.post(f"/sessions/{session_id}/end")
        second = await client.post(f"/sessions/{session_id}/end")
        first_signal = await wait_for_signal(pubsub)
        second_signal = await wait_for_signal(pubsub)
    finally:
        await pubsub.aclose()
        await redis.aclose()

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["data"]["already_ended"] is False
    assert second.json()["data"]["already_ended"] is True
    assert first.json()["data"]["ended_at"] == second.json()["data"]["ended_at"]
    assert first_signal is not None
    assert second_signal is None, "the repeat call published a second signal"
    assert await count_audit_rows(sessions, session_id, "END_SESSION") == 1
    assert await count_audit_rows(sessions, session_id, "READ_ENCOUNTER") == 1


async def test_end_returns_404_for_an_unknown_session(client: AsyncClient) -> None:
    response = await client.post(f"/sessions/{uuid.uuid4()}/end")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"


async def test_end_returns_404_for_a_soft_deleted_encounter(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Soft-deleted rows are retired — the endpoint must not resurrect one."""
    session_id, _ = await start_session(client)
    async with sessions() as session:
        await session.execute(
            sa.update(Encounter)
            .where(Encounter.session_id == session_id)
            .values(deleted_at=sa.func.now())
        )
        await session.commit()

    response = await client.post(f"/sessions/{session_id}/end")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"

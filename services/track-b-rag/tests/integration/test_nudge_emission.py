"""The nudge write against a real database, including the path a retry takes.

What a fake cannot prove:

* **The ``ON CONFLICT`` target matches migration 0005's partial unique index.**
  PostgreSQL infers a partial index only when the predicate is supplied along
  with the columns, and getting that wrong raises at execution time rather than
  at import. A fake insert would agree with whatever the emitter asked it for.
* **A retry republishes rather than duplicating.** This is the whole reason the
  store-then-publish order is safe: the emitter writes the row first because the
  payload carries its id, so a failed publish leaves a row behind and the
  consumer hands the dedup claim back. The second attempt has to find that row.
* **The audit write joins the insert's transaction.** ``audit_log`` runs on the
  connection the nudge is written on, and the two committing or rolling back
  together is a property of the database session, not of the call.

Skipped when DATABASE_URL is unset, so the unit suite still runs on a machine
with nothing up. Each test writes its own encounter and removes it afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from track_a_clinical.models import ClinicalNudge, Encounter
from track_b_rag import db, nudges
from track_b_rag.api.schemas import PolicyQueryData
from track_b_rag.policy_dispatch import PolicyQueryParameters

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="needs a real PostgreSQL (DATABASE_URL)",
    ),
]


def database_url() -> str:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def answer(**overrides: Any) -> PolicyQueryData:
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
    """Publishes nowhere. The database is what these tests are about."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def use_the_real_sessionmaker(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Point the emitter's sessionmaker at this test's engine.

    ``get_sessionmaker`` is lru_cached on the process and the unit suite may
    have populated it, so building one here keeps these tests independent of
    run order.
    """
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "get_sessionmaker", lambda: factory, raising=True)
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def encounter(session: AsyncSession) -> AsyncIterator[Encounter]:
    """A real encounter, with its nudges, removed afterwards."""
    row = Encounter(
        session_id=uuid.uuid4(),
        patient_fhir_id="Patient/synthetic-task-040",
        provider_id=uuid.uuid4(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    yield row
    await session.execute(sa.delete(ClinicalNudge).where(ClinicalNudge.encounter_id == row.id))
    await session.execute(sa.delete(Encounter).where(Encounter.id == row.id))
    await session.commit()


def parameters(encounter: Encounter, *, cpt_code: str = "73721") -> PolicyQueryParameters:
    return PolicyQueryParameters(
        procedure="knee MRI",
        cpt_code=cpt_code,
        payer="Aetna",
        plan_type="PPO",
        state="MA",
        provider_id=encounter.provider_id,
        encounter_id=encounter.id,
    )


async def nudge_rows(session: AsyncSession, encounter: Encounter) -> list[ClinicalNudge]:
    result = await session.execute(
        sa.select(ClinicalNudge).where(ClinicalNudge.encounter_id == encounter.id)
    )
    return list(result.scalars())


async def audit_rows(session: AsyncSession, nudge_id: uuid.UUID) -> list[Any]:
    result = await session.execute(
        sa.text(
            "SELECT actor_id, action, resource_type, resource_id, session_id, "
            "service_name, ip_address, user_agent FROM audit_log "
            "WHERE resource_id = :resource_id AND action = 'WRITE_NUDGE'"
        ),
        {"resource_id": str(nudge_id)},
    )
    return list(result.all())


async def test_a_nudge_is_stored_with_everything_the_answer_carried(
    session: AsyncSession, encounter: Encounter
) -> None:
    redis = FakeRedis()

    nudge_id = await nudges.emit(
        redis=redis,  # type: ignore[arg-type]
        session_id=encounter.session_id,
        parameters=parameters(encounter),
        answer=answer(),
    )

    (row,) = await nudge_rows(session, encounter)
    assert row.id == nudge_id
    assert row.cpt_code == "73721"
    assert row.procedure_name == "knee MRI"
    assert row.missing_criteria == ["Failed six weeks of conservative therapy"]
    assert row.denial_risk == "high"
    # The provenance TASK-040 threaded out of Stage 1 for exactly this column.
    assert row.payer_policy_source == "L33575"
    assert row.acknowledged is False


async def test_the_write_is_audited_as_the_encounters_provider(
    session: AsyncSession, encounter: Encounter
) -> None:
    """CLAUDE.md, "Auditing work that no request triggered".

    No request, so no client: the actor is the provider recorded on the
    encounter, and the address and user agent are absent permanently rather
    than pending the middleware that fills them in for routes.
    """
    redis = FakeRedis()

    nudge_id = await nudges.emit(
        redis=redis,  # type: ignore[arg-type]
        session_id=encounter.session_id,
        parameters=parameters(encounter),
        answer=answer(),
    )

    assert nudge_id is not None
    (row,) = await audit_rows(session, nudge_id)
    assert row.actor_id == encounter.provider_id
    assert row.action == "WRITE_NUDGE"
    assert row.resource_type == "ClinicalNudge"
    assert row.session_id == encounter.session_id
    assert row.service_name == "track-b-rag"
    assert row.ip_address is None
    assert row.user_agent is None


async def test_the_audit_row_carries_no_clinical_detail(
    session: AsyncSession, encounter: Encounter
) -> None:
    """An audit row says an access happened. The nudge row holds what it said."""
    redis = FakeRedis()

    nudge_id = await nudges.emit(
        redis=redis,  # type: ignore[arg-type]
        session_id=encounter.session_id,
        parameters=parameters(encounter),
        answer=answer(),
    )

    assert nudge_id is not None
    (row,) = await audit_rows(session, nudge_id)
    rendered = " ".join(str(value) for value in row)
    assert "73721" not in rendered
    assert "knee MRI" not in rendered
    assert "conservative therapy" not in rendered


async def test_a_retry_republishes_the_same_nudge_and_writes_no_second_row(
    session: AsyncSession, encounter: Encounter
) -> None:
    """The reason store-then-publish is safe.

    A publish that failed leaves the row behind and the consumer gives its
    dedup claim back, so the next mention of the procedure arrives here again.
    It has to find the existing nudge — a second row would show the provider
    the same alert twice and give TASK-041b two ids for one thing to acknowledge.
    """
    redis = FakeRedis()

    first = await nudges.emit(
        redis=redis,  # type: ignore[arg-type]
        session_id=encounter.session_id,
        parameters=parameters(encounter),
        answer=answer(),
    )
    second = await nudges.emit(
        redis=redis,  # type: ignore[arg-type]
        session_id=encounter.session_id,
        parameters=parameters(encounter),
        answer=answer(),
    )

    assert first == second
    assert len(await nudge_rows(session, encounter)) == 1
    # Published both times: the retry exists because the first publish failed.
    assert len(redis.published) == 2


async def test_a_republish_writes_no_second_audit_row(
    session: AsyncSession, encounter: Encounter
) -> None:
    """One row per unit of work. The nudge was raised once and recorded once."""
    redis = FakeRedis()

    nudge_id = await nudges.emit(
        redis=redis,  # type: ignore[arg-type]
        session_id=encounter.session_id,
        parameters=parameters(encounter),
        answer=answer(),
    )
    await nudges.emit(
        redis=redis,  # type: ignore[arg-type]
        session_id=encounter.session_id,
        parameters=parameters(encounter),
        answer=answer(),
    )

    assert nudge_id is not None
    assert len(await audit_rows(session, nudge_id)) == 1


async def test_two_procedures_in_one_encounter_each_get_a_nudge(
    session: AsyncSession, encounter: Encounter
) -> None:
    """The constraint is per procedure, not per encounter."""
    redis = FakeRedis()

    await nudges.emit(
        redis=redis,  # type: ignore[arg-type]
        session_id=encounter.session_id,
        parameters=parameters(encounter, cpt_code="73721"),
        answer=answer(),
    )
    await nudges.emit(
        redis=redis,  # type: ignore[arg-type]
        session_id=encounter.session_id,
        parameters=parameters(encounter, cpt_code="70551"),
        answer=answer(),
    )

    assert len(await nudge_rows(session, encounter)) == 2

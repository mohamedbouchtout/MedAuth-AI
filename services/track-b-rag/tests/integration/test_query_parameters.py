"""TASK-024's parameter resolution against a real ``encounters`` row.

What a fake cannot prove:

* **The column exists and round-trips.** ``encounters.state`` is added by
  track-a-clinical's ``0003_encounter_state`` migration and read here by a
  service that does not own it. A unit test's fake row agrees with whatever the
  test hands it, so only a real SELECT against a migrated database shows the two
  halves of that cross-service arrangement lining up.
* **The SELECT reads the four columns it claims to.** The decision that this is
  not a PHI access rests on the query naming ``provider_id``,
  ``insurance_payer``, ``insurance_plan_type`` and ``state`` and nothing else.
  That is a property of the statement SQLAlchemy actually emits.
* **A populated encounter resolves every parameter.** The row here is written
  directly rather than through a SMART launch, which keeps this module about the
  resolution alone — it was what de-risked TASK-052b before that task landed, and
  it is now the narrow test beneath ``test_end_to_end_nudge.py``, which populates
  the same columns from a real FHIR server and runs the whole chain on them.

Skipped when DATABASE_URL is unset, so the unit suite still runs on a machine
with nothing up. Each test writes its own encounter and deletes it afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from track_a_clinical.models import Encounter
from track_b_rag import policy_dispatch
from track_b_rag.keywords import ProcedureMention
from track_b_rag.policy_dispatch import MissingQueryParameters, resolve_query_parameters

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="needs a real PostgreSQL (DATABASE_URL)",
    ),
]

KNEE_MRI = ProcedureMention(
    keyword="MRI",
    procedure="MRI",
    matched_text="MRI",
    excerpt="The knee has been locking for months. Let's order an MRI of the knee.",
)


def database_url() -> str:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session against the real database."""
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def use_the_real_sessionmaker(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Point the module's sessionmaker at this test's engine.

    ``get_sessionmaker`` is lru_cached on the process, and the unit suite may
    have populated it; building one here keeps these tests independent of run
    order.
    """
    engine = create_async_engine(database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(policy_dispatch, "get_sessionmaker", lambda: factory, raising=True)
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def encounter(session: AsyncSession) -> AsyncIterator[Encounter]:
    """A real encounter row, removed afterwards.

    Hard-deleted rather than soft-deleted on cleanup: this is test data that was
    never a real visit, and the soft-delete convention exists so a clinical
    record survives, not so test rows accumulate.
    """
    row = Encounter(
        session_id=uuid.uuid4(),
        patient_fhir_id="Patient/synthetic-task-024",
        provider_id=uuid.uuid4(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    yield row
    await session.execute(sa.delete(Encounter).where(Encounter.id == row.id))
    await session.commit()


async def test_a_populated_encounter_resolves_every_parameter(
    session: AsyncSession, encounter: Encounter
) -> None:
    """The resolution alone, on a row written directly rather than from a launch."""
    encounter.insurance_payer = "Blue Cross Blue Shield of Massachusetts"
    encounter.insurance_plan_type = "PPO"
    encounter.state = "MA"
    await session.commit()

    parameters = await resolve_query_parameters(session_id=encounter.session_id, mention=KNEE_MRI)

    assert parameters.cpt_code == "73721"
    assert parameters.procedure == "MRI of a lower extremity joint"
    assert parameters.payer == "Blue Cross Blue Shield of Massachusetts"
    assert parameters.plan_type == "PPO"
    assert parameters.state == "MA"
    assert parameters.provider_id == encounter.provider_id


async def test_the_state_column_round_trips_through_the_migration(
    session: AsyncSession, encounter: Encounter
) -> None:
    """TASK-024: the migration adds `state` and the model round-trips it.

    Read back with a fresh SELECT rather than off the instance, so this is the
    database's value and not SQLAlchemy's identity map.
    """
    encounter.state = "MA"
    await session.commit()
    session.expunge_all()

    stored = await session.scalar(sa.select(Encounter.state).where(Encounter.id == encounter.id))

    assert stored == "MA"


async def test_an_encounter_with_empty_payer_columns_refuses(encounter: Encounter) -> None:
    """What every encounter looks like today. It names all three, not a generic gap."""
    with pytest.raises(MissingQueryParameters) as raised:
        await resolve_query_parameters(session_id=encounter.session_id, mention=KNEE_MRI)

    assert raised.value.fields == ("payer", "plan_type", "state")


async def test_a_soft_deleted_encounter_is_not_found(
    session: AsyncSession, encounter: Encounter
) -> None:
    """The `deleted_at IS NULL` half of the WHERE clause, against real SQL."""
    encounter.insurance_payer = "Aetna"
    encounter.insurance_plan_type = "PPO"
    encounter.state = "MA"
    encounter.deleted_at = sa.func.now()
    await session.commit()

    with pytest.raises(MissingQueryParameters) as raised:
        await resolve_query_parameters(session_id=encounter.session_id, mention=KNEE_MRI)

    assert raised.value.reason == "no active encounter for this session"


async def test_an_unknown_session_is_not_found() -> None:
    with pytest.raises(MissingQueryParameters) as raised:
        await resolve_query_parameters(session_id=uuid.uuid4(), mention=KNEE_MRI)

    assert raised.value.reason == "no active encounter for this session"


async def test_the_statement_reads_no_patient_column(
    encounter: Encounter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit decision, enforced by the query rather than by convention.

    If a later change swaps the explicit column list for `select(Encounter)`,
    this fails — which is the point. Reading `patient_fhir_id` here would make
    this a PHI access needing its own audit_log() row, and the whole reason the
    seam does not write one is that it reads none of these.
    """
    captured: list[str] = []
    factory = policy_dispatch.get_sessionmaker()

    class Recording:
        """Wraps a real session so the statement is both recorded and executed."""

        async def __aenter__(self) -> Recording:
            self._session = factory()
            await self._session.__aenter__()
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            await self._session.__aexit__(*exc_info)

        async def execute(self, statement: sa.Select[object]) -> object:
            captured.append(str(statement.compile()))
            return await self._session.execute(statement)

    monkeypatch.setattr(policy_dispatch, "get_sessionmaker", lambda: Recording, raising=True)

    with pytest.raises(MissingQueryParameters):
        await resolve_query_parameters(session_id=encounter.session_id, mention=KNEE_MRI)

    (sql,) = captured
    assert "provider_id" in sql
    assert "insurance_payer" in sql
    assert "patient_fhir_id" not in sql
    assert "insurance_member_id" not in sql
    assert "ehr_encounter_id" not in sql

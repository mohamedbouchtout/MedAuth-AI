"""The prior-auth queue against a real PostgreSQL (TASK-072).

The unit suite covers what leaves the service: which fields the row carries, and
that the list writes no audit row while the decision read does. None of that is
what a database is needed for.

What only PostgreSQL can show is the query itself, and it is the half most able
to be wrong while looking right:

* **The provider filter.** A list that silently returned another provider's
  requests would render as a working dashboard. This is the widest read in the
  service, so the scoping is the thing most worth proving.
* **The cursor.** Row-wise comparison against a composite sort key is exactly the
  kind of SQL that appears to work on a handful of rows and then skips or repeats
  one at a page boundary. The tie case — two encounters sharing a ``started_at``
  — is the one a timestamp-only cursor gets wrong, and it is why the request id
  is in the key at all.
* **The status filter**, including ``manual-submission-required``, which is a
  value the free-text status column takes and no enum constrains.

Skipped when DATABASE_URL is unset, like the rest of this suite.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from track_a_clinical import prior_auth
from track_a_clinical.db import database_url
from track_a_clinical.models import (
    ENCOUNTER_STATUS_COMPLETED,
    PRIOR_AUTH_STATUS_DENIED,
    PRIOR_AUTH_STATUS_MANUAL_REQUIRED,
    PRIOR_AUTH_STATUS_PENDING,
    Encounter,
    PriorAuthRequest,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL is not set — the queue query needs a real PostgreSQL",
    ),
]

PATIENT_FHIR_ID = "synthea-placeholder-72"

#: A fixed base time rather than ``now()``, so the ordering asserted here is the
#: ordering the rows were written with and not a race against the clock.
BASE_TIME = datetime.datetime(2026, 4, 1, 9, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url())
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Fixtures:
    """Rows written for one test, and the ids needed to clean them up."""

    def __init__(self) -> None:
        self.encounter_ids: list[uuid.UUID] = []
        self.request_ids: list[uuid.UUID] = []


@pytest_asyncio.fixture
async def rows(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Fixtures]:
    created = Fixtures()

    yield created

    async with sessions() as session:
        await session.execute(
            sa.delete(PriorAuthRequest).where(PriorAuthRequest.id.in_(created.request_ids))
        )
        await session.execute(sa.delete(Encounter).where(Encounter.id.in_(created.encounter_ids)))
        await session.commit()


async def write_request(
    sessions: async_sessionmaker[AsyncSession],
    created: Fixtures,
    *,
    provider_id: uuid.UUID,
    started_at: datetime.datetime,
    status: str = PRIOR_AUTH_STATUS_PENDING,
) -> PriorAuthRequest:
    """Insert one encounter and the single request TASK-060 would have assembled for it."""
    encounter = Encounter(
        session_id=uuid.uuid4(),
        patient_fhir_id=PATIENT_FHIR_ID,
        provider_id=provider_id,
        status=ENCOUNTER_STATUS_COMPLETED,
        started_at=started_at,
    )
    async with sessions() as session:
        session.add(encounter)
        await session.commit()
        await session.refresh(encounter)

        request = PriorAuthRequest(
            encounter_id=encounter.id,
            status=status,
            payer_name="Aetna",
        )
        session.add(request)
        await session.commit()
        await session.refresh(request)

    created.encounter_ids.append(encounter.id)
    created.request_ids.append(request.id)
    return request


async def test_the_list_is_scoped_to_one_provider(
    sessions: async_sessionmaker[AsyncSession], rows: Fixtures
) -> None:
    """The property this route's whole design rests on.

    An unscoped list would render as a working dashboard, which is exactly why
    it is asserted against a database holding another provider's row rather than
    against a fake that was only ever given one.
    """
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    kept = await write_request(sessions, rows, provider_id=mine, started_at=BASE_TIME)
    await write_request(sessions, rows, provider_id=theirs, started_at=BASE_TIME)

    async with sessions() as session:
        page, cursor = await prior_auth.list_requests(session, provider_id=mine)

    assert [request.id for request, _ in page] == [kept.id]
    assert cursor is None


async def test_a_soft_deleted_encounter_is_absent(
    sessions: async_sessionmaker[AsyncSession], rows: Fixtures
) -> None:
    """Consistent with every other read here: a retired encounter is not found."""
    provider_id = uuid.uuid4()
    await write_request(sessions, rows, provider_id=provider_id, started_at=BASE_TIME)

    async with sessions() as session:
        await session.execute(
            sa.update(Encounter)
            .where(Encounter.id == rows.encounter_ids[0])
            .values(deleted_at=sa.func.now())
        )
        await session.commit()

    async with sessions() as session:
        page, _ = await prior_auth.list_requests(session, provider_id=provider_id)

    assert page == []


async def test_newest_visit_first(
    sessions: async_sessionmaker[AsyncSession], rows: Fixtures
) -> None:
    provider_id = uuid.uuid4()
    older = await write_request(sessions, rows, provider_id=provider_id, started_at=BASE_TIME)
    newer = await write_request(
        sessions,
        rows,
        provider_id=provider_id,
        started_at=BASE_TIME + datetime.timedelta(hours=2),
    )

    async with sessions() as session:
        page, _ = await prior_auth.list_requests(session, provider_id=provider_id)

    assert [request.id for request, _ in page] == [newer.id, older.id]


async def test_the_status_filter_selects_one_state(
    sessions: async_sessionmaker[AsyncSession], rows: Fixtures
) -> None:
    """Including ``manual-submission-required``, which no enum constrains.

    It is the value a provider filters on to find the work that is theirs to do
    by hand, so a filter that silently matched nothing would hide exactly the
    queue this status exists to make visible.
    """
    provider_id = uuid.uuid4()
    await write_request(sessions, rows, provider_id=provider_id, started_at=BASE_TIME)
    manual = await write_request(
        sessions,
        rows,
        provider_id=provider_id,
        started_at=BASE_TIME + datetime.timedelta(hours=1),
        status=PRIOR_AUTH_STATUS_MANUAL_REQUIRED,
    )
    await write_request(
        sessions,
        rows,
        provider_id=provider_id,
        started_at=BASE_TIME + datetime.timedelta(hours=3),
        status=PRIOR_AUTH_STATUS_DENIED,
    )

    async with sessions() as session:
        page, _ = await prior_auth.list_requests(
            session, provider_id=provider_id, status=PRIOR_AUTH_STATUS_MANUAL_REQUIRED
        )

    assert [request.id for request, _ in page] == [manual.id]


async def test_paging_returns_each_request_exactly_once(
    sessions: async_sessionmaker[AsyncSession], rows: Fixtures
) -> None:
    provider_id = uuid.uuid4()
    written = [
        await write_request(
            sessions,
            rows,
            provider_id=provider_id,
            started_at=BASE_TIME + datetime.timedelta(hours=offset),
        )
        for offset in range(5)
    ]

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    async with sessions() as session:
        while True:
            page, cursor = await prior_auth.list_requests(
                session, provider_id=provider_id, cursor=cursor, limit=2
            )
            seen.extend(request.id for request, _ in page)
            if cursor is None:
                break

    assert sorted(seen, key=str) == sorted((request.id for request in written), key=str)
    assert len(seen) == len(set(seen))


async def test_paging_is_stable_when_two_visits_share_a_start_time(
    sessions: async_sessionmaker[AsyncSession], rows: Fixtures
) -> None:
    """The case a timestamp-only cursor gets wrong, which is why the id is in the key.

    With the page boundary falling between two rows carrying the same
    ``started_at``, a cursor naming only the timestamp either skips the second
    one or serves it again. Both are silent.
    """
    provider_id = uuid.uuid4()
    written = [
        await write_request(sessions, rows, provider_id=provider_id, started_at=BASE_TIME)
        for _ in range(4)
    ]

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    async with sessions() as session:
        while True:
            page, cursor = await prior_auth.list_requests(
                session, provider_id=provider_id, cursor=cursor, limit=2
            )
            seen.extend(request.id for request, _ in page)
            if cursor is None:
                break

    assert sorted(seen, key=str) == sorted((request.id for request in written), key=str)
    assert len(seen) == len(set(seen))


async def test_a_full_page_with_nothing_after_it_reports_no_cursor(
    sessions: async_sessionmaker[AsyncSession], rows: Fixtures
) -> None:
    """The off-by-one a ``LIMIT n`` implementation gets wrong.

    Fetching one more row than the page is what answers "is there a next page"
    without a second query, and the boundary to prove is a page that is exactly
    full: a cursor here would send a client after an empty page, and reporting
    one only when a page is short would be the same bug in the other direction.
    """
    provider_id = uuid.uuid4()
    for offset in range(2):
        await write_request(
            sessions,
            rows,
            provider_id=provider_id,
            started_at=BASE_TIME + datetime.timedelta(hours=offset),
        )

    async with sessions() as session:
        page, cursor = await prior_auth.list_requests(session, provider_id=provider_id, limit=2)

    assert len(page) == 2
    assert cursor is None

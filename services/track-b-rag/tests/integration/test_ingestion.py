"""The three dedup claims TASK-011 names, against real Qdrant and real Postgres.

These cannot be proven against fakes. "The second ingest does not duplicate
points" and "the old points are removed" are statements about what a running
Qdrant holds afterwards, and a fake will report whatever it was told to. Same for
the ``insurance_policies`` row: the ``ON CONFLICT DO UPDATE`` path only exists
against a real unique constraint.

The embedding model is stubbed with deterministic vectors of the right width.
That is deliberate — what is under test here is the ingestion pipeline's effect
on the two stores, not the quality of the embeddings, and requiring the real
1.3 GB weights would put these behind ``RUN_EMBEDDING_TESTS`` and leave the
dedup claims unverified on most runs. TASK-010's integration suite covers the
real model.

Skipped when QDRANT_HOST or DATABASE_URL is unset, so the unit suite still runs
on a machine with nothing up. Each test uses its own collection and its own
policy_id, and cleans up after itself, so a shared CI container is not left
dirty for the next member.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pymupdf
import pytest
import pytest_asyncio
import sqlalchemy as sa
from qdrant_client import QdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from payer_vocab import normalize_payer
from track_a_clinical.models import InsurancePolicy
from track_b_rag import embeddings
from track_b_rag.ingestion import PolicyMetadata, ingest_policy
from track_b_rag.retrieval import policy_query_filter
from track_b_rag.vector_store import (
    count_policy_points,
    ensure_collection,
    ensure_payload_indexes,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("QDRANT_HOST") or not os.environ.get("DATABASE_URL"),
        reason="needs a real Qdrant and PostgreSQL (QDRANT_HOST and DATABASE_URL)",
    ),
]

VECTOR_SIZE = 1024


def build_pdf(pages: list[str]) -> bytes:
    """Return a real multi-page PDF carrying the given text."""
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    for text in pages:
        page = document.new_page()
        # Several lines per page, so the document chunks into more than one piece.
        for line, offset in enumerate(range(72, 600, 24)):
            page.insert_text((72, offset), f"{text} — line {line}. " * 3)
    data: bytes = document.tobytes()
    document.close()
    return data


def ten_page_policy(marker: str = "Prior authorization criteria") -> bytes:
    return build_pdf([f"{marker}, page {page}" for page in range(1, 11)])


@pytest.fixture(autouse=True)
def stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic unit-length vectors of the collection's width."""

    def encode(texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (VECTOR_SIZE - 1) for _ in texts]

    monkeypatch.setattr(embeddings, "embed_documents", encode)


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    client = QdrantClient(
        host=os.environ["QDRANT_HOST"],
        port=int(os.environ.get("QDRANT_PORT", "6333")),
        api_key=os.environ.get("QDRANT_API_KEY") or None,
    )
    yield client
    client.close()


@pytest.fixture
def collection(qdrant: QdrantClient) -> Iterator[str]:
    """A collection of this test's own, created and torn down here."""
    name = f"test_ingest_{uuid.uuid4().hex}"
    ensure_collection(qdrant, name, VECTOR_SIZE)
    ensure_payload_indexes(qdrant, name)
    yield name
    qdrant.delete_collection(name)


@pytest.fixture
def policy_id() -> str:
    return f"TEST-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def session(policy_id: str) -> AsyncIterator[AsyncSession]:
    """A session against the real database, with this test's row removed after."""
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as db:
        yield db
        await db.execute(sa.delete(InsurancePolicy).where(InsurancePolicy.policy_id == policy_id))
        await db.commit()
    await engine.dispose()


def metadata_for(policy_id: str) -> PolicyMetadata:
    return PolicyMetadata(
        policy_id=policy_id,
        payer="CMS",
        plan_type="Medicare",
        state="NY",
        source_url="https://example.gov/policy.pdf",
        effective_date=datetime.date(2026, 1, 1),
    )


async def ingest(
    session: AsyncSession,
    qdrant: QdrantClient,
    collection: str,
    policy_id: str,
    pdf: bytes,
) -> object:
    return await ingest_policy(
        session=session,
        client=qdrant,
        collection=collection,
        pdf_bytes=pdf,
        metadata=metadata_for(policy_id),
    )


# --- TASK-011: ingest a sample 10-page PDF, verify chunks appear in Qdrant ---


async def test_a_ten_page_pdf_lands_as_chunks_in_qdrant(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    result = await ingest(session, qdrant, collection, policy_id, ten_page_policy())

    assert result.status == "created"  # type: ignore[attr-defined]
    assert result.chunks_indexed > 1  # type: ignore[attr-defined]
    assert count_policy_points(qdrant, collection, policy_id) == result.chunks_indexed  # type: ignore[attr-defined]


async def test_the_indexed_points_carry_the_payload_schema(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    await ingest(session, qdrant, collection, policy_id, ten_page_policy())

    points, _ = qdrant.scroll(collection_name=collection, limit=1, with_payload=True)
    payload = points[0].payload or {}
    assert payload["policy_id"] == policy_id
    # The slug, not the display name — this is what the retrieval filter matches
    # by exact equality (TASK-016).
    assert payload["payer"] == "cms-medicare"
    assert payload["state"] == "NY"
    assert payload["text"]


async def test_the_policy_row_records_the_ingest(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    result = await ingest(session, qdrant, collection, policy_id, ten_page_policy())

    row = await session.scalar(
        sa.select(InsurancePolicy).where(InsurancePolicy.policy_id == policy_id)
    )
    assert row is not None
    assert row.content_hash == result.content_hash  # type: ignore[attr-defined]
    assert row.qdrant_collection == collection
    # The row keeps the payer's own spelling; nothing matches on this column.
    assert row.payer == "CMS"
    assert row.source_url == "https://example.gov/policy.pdf"
    assert row.effective_date == datetime.date(2026, 1, 1)


async def test_the_payload_slug_is_what_a_query_filter_would_match(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    """The two halves of TASK-016 meeting: a document ingested as "CMS" is found by
    a query whose payer arrived as a FHIR Coverage display name."""
    await ingest(session, qdrant, collection, policy_id, ten_page_policy())

    points, _ = qdrant.scroll(
        collection_name=collection,
        scroll_filter=policy_query_filter(payer=normalize_payer("Medicare Part B"), state="NY"),
        limit=1,
        with_payload=True,
    )

    assert points, "a query spelling the payer differently found nothing"
    assert (points[0].payload or {})["policy_id"] == policy_id


# --- TASK-011: the same PDF twice is "unchanged" and does not duplicate ------


async def test_the_same_pdf_twice_is_unchanged(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    pdf = ten_page_policy()
    await ingest(session, qdrant, collection, policy_id, pdf)

    second = await ingest(session, qdrant, collection, policy_id, pdf)

    assert second.status == "unchanged"  # type: ignore[attr-defined]
    assert second.chunks_indexed == 0  # type: ignore[attr-defined]


async def test_the_same_pdf_twice_does_not_duplicate_points(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    pdf = ten_page_policy()
    first = await ingest(session, qdrant, collection, policy_id, pdf)
    before = count_policy_points(qdrant, collection, policy_id)

    await ingest(session, qdrant, collection, policy_id, pdf)

    assert count_policy_points(qdrant, collection, policy_id) == before == first.chunks_indexed  # type: ignore[attr-defined]


async def test_an_unchanged_ingest_leaves_the_row_alone(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    """A skipped ingest does not bump last_ingested_at — nothing was ingested."""
    pdf = ten_page_policy()
    await ingest(session, qdrant, collection, policy_id, pdf)
    original = await session.scalar(
        sa.select(InsurancePolicy.last_ingested_at).where(InsurancePolicy.policy_id == policy_id)
    )

    await ingest(session, qdrant, collection, policy_id, pdf)

    current = await session.scalar(
        sa.select(InsurancePolicy.last_ingested_at).where(InsurancePolicy.policy_id == policy_id)
    )
    assert current == original


# --- TASK-011: a modified policy removes the old points and reports "updated" ---


async def test_a_modified_policy_is_updated(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    await ingest(session, qdrant, collection, policy_id, ten_page_policy())

    result = await ingest(
        session, qdrant, collection, policy_id, ten_page_policy("Revised criteria")
    )

    assert result.status == "updated"  # type: ignore[attr-defined]


async def test_a_modified_policy_replaces_rather_than_accumulates(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    await ingest(session, qdrant, collection, policy_id, ten_page_policy())

    result = await ingest(
        session, qdrant, collection, policy_id, ten_page_policy("Revised criteria")
    )

    assert count_policy_points(qdrant, collection, policy_id) == result.chunks_indexed  # type: ignore[attr-defined]


async def test_the_superseded_text_is_gone_from_the_collection(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    """The point of deleting first: an old revision must not stay retrievable."""
    await ingest(session, qdrant, collection, policy_id, ten_page_policy("ORIGINALMARKER"))

    await ingest(session, qdrant, collection, policy_id, ten_page_policy("REVISEDMARKER"))

    points, _ = qdrant.scroll(collection_name=collection, limit=1000, with_payload=True)
    texts = " ".join(str((point.payload or {}).get("text", "")) for point in points)
    assert "ORIGINALMARKER" not in texts
    assert "REVISEDMARKER" in texts


async def test_a_shorter_revision_strands_nothing(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    """Deleting by filter, not by reconstructed point ID, is what makes this hold."""
    await ingest(session, qdrant, collection, policy_id, ten_page_policy())

    result = await ingest(session, qdrant, collection, policy_id, build_pdf(["Short policy."]))

    assert count_policy_points(qdrant, collection, policy_id) == result.chunks_indexed  # type: ignore[attr-defined]


async def test_an_update_refreshes_the_stored_digest(
    session: AsyncSession, qdrant: QdrantClient, collection: str, policy_id: str
) -> None:
    first = await ingest(session, qdrant, collection, policy_id, ten_page_policy())

    second = await ingest(
        session, qdrant, collection, policy_id, ten_page_policy("Revised criteria")
    )

    row = await session.scalar(
        sa.select(InsurancePolicy).where(InsurancePolicy.policy_id == policy_id)
    )
    assert row is not None
    assert row.content_hash == second.content_hash != first.content_hash  # type: ignore[attr-defined]

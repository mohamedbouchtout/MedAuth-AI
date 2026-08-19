"""The three-way dedup, and the write ordering that decides how it fails.

Postgres and Qdrant are both faked here. The claims that need real containers —
that chunks are actually retrievable, that a re-ingest does not duplicate points
— are in ``tests/integration/test_ingestion.py``; these cover the decision logic
around them, which a container cannot make legible.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from track_b_rag import embeddings, ingestion
from track_b_rag.ingestion import (
    EmptyDocumentError,
    PolicyMetadata,
    ingest_policy,
)
from track_b_rag.pdf import PdfParseError, content_digest

COLLECTION = "insurance_policies"

METADATA = PolicyMetadata(
    policy_id="L33575",
    payer="CMS",
    plan_type="Medicare",
    state="NY",
    source_url="https://example.gov/L33575.pdf",
    effective_date=datetime.date(2026, 1, 1),
)


class FakeRow:
    """An existing insurance_policies row, as far as this module reads one."""

    def __init__(self, content_hash: str) -> None:
        self.content_hash = content_hash


class FakeSession:
    """Records the statement it was handed and whether the work was committed."""

    def __init__(self, existing: FakeRow | None = None) -> None:
        self.existing = existing
        self.executed: list[Any] = []
        self.commits = 0

    async def scalar(self, statement: Any) -> FakeRow | None:
        return self.existing

    async def execute(self, statement: Any) -> None:
        self.executed.append(statement)

    async def commit(self) -> None:
        self.commits += 1


class FakeQdrant:
    """Records the ordering of the writes, which is what this task fixes."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.deleted: list[str] = []
        self.upserted: list[Any] = []


@pytest.fixture
def qdrant(monkeypatch: pytest.MonkeyPatch) -> FakeQdrant:
    client = FakeQdrant()

    def delete(passed: Any, name: str, policy_id: str) -> None:
        client.calls.append("delete")
        client.deleted.append(policy_id)

    def upsert(passed: Any, name: str, points: Any) -> None:
        client.calls.append("upsert")
        client.upserted.extend(points)

    monkeypatch.setattr(ingestion.vector_store, "delete_policy_points", delete)
    monkeypatch.setattr(ingestion.vector_store, "upsert_points", upsert)
    return client


@pytest.fixture(autouse=True)
def stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """One deterministic vector per chunk — the model itself is TASK-010's concern."""
    monkeypatch.setattr(
        embeddings,
        "embed_documents",
        lambda texts: [[float(index)] for index, _ in enumerate(texts)],
    )


@pytest.fixture(autouse=True)
def stub_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parse and chunk without building a real PDF; test_pdf.py covers the real thing."""
    monkeypatch.setattr(ingestion, "extract_text", lambda data: data.decode())
    monkeypatch.setattr(ingestion, "chunk_text", lambda text: text.split("|") if text else [])


PDF = b"first chunk|second chunk|third chunk"


async def ingest(session: FakeSession, client: FakeQdrant, pdf: bytes = PDF) -> Any:
    return await ingest_policy(
        session=session,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        collection=COLLECTION,
        pdf_bytes=pdf,
        metadata=METADATA,
    )


# --- created ---------------------------------------------------------------


async def test_a_new_policy_is_created(qdrant: FakeQdrant) -> None:
    result = await ingest(FakeSession(), qdrant)

    assert result.status == "created"


async def test_a_new_policy_indexes_every_chunk(qdrant: FakeQdrant) -> None:
    result = await ingest(FakeSession(), qdrant)

    assert result.chunks_indexed == 3
    assert len(qdrant.upserted) == 3


async def test_a_new_policy_deletes_nothing(qdrant: FakeQdrant) -> None:
    """There is nothing to remove, and a delete on an absent policy is not free."""
    await ingest(FakeSession(), qdrant)

    assert qdrant.deleted == []


async def test_the_result_carries_the_digest_of_the_uploaded_bytes(qdrant: FakeQdrant) -> None:
    result = await ingest(FakeSession(), qdrant)

    assert result.content_hash == content_digest(PDF)


async def test_the_result_names_the_collection(qdrant: FakeQdrant) -> None:
    result = await ingest(FakeSession(), qdrant)

    assert result.collection == COLLECTION


async def test_the_row_is_written_and_committed(qdrant: FakeQdrant) -> None:
    session = FakeSession()

    await ingest(session, qdrant)

    assert len(session.executed) == 1
    assert session.commits == 1


# --- unchanged -------------------------------------------------------------


async def test_a_matching_digest_is_unchanged(qdrant: FakeQdrant) -> None:
    session = FakeSession(FakeRow(content_digest(PDF)))

    result = await ingest(session, qdrant)

    assert result.status == "unchanged"


async def test_an_unchanged_policy_does_no_qdrant_work(qdrant: FakeQdrant) -> None:
    session = FakeSession(FakeRow(content_digest(PDF)))

    await ingest(session, qdrant)

    assert qdrant.calls == []


async def test_an_unchanged_policy_writes_no_row(qdrant: FakeQdrant) -> None:
    """Skipping means skipping: last_ingested_at is not bumped for a no-op."""
    session = FakeSession(FakeRow(content_digest(PDF)))

    await ingest(session, qdrant)

    assert session.executed == []
    assert session.commits == 0


async def test_an_unchanged_policy_reports_zero_chunks(qdrant: FakeQdrant) -> None:
    session = FakeSession(FakeRow(content_digest(PDF)))

    result = await ingest(session, qdrant)

    assert result.chunks_indexed == 0


async def test_an_unchanged_policy_still_reports_its_digest(qdrant: FakeQdrant) -> None:
    session = FakeSession(FakeRow(content_digest(PDF)))

    result = await ingest(session, qdrant)

    assert result.content_hash == content_digest(PDF)


# --- updated ---------------------------------------------------------------


async def test_a_differing_digest_is_updated(qdrant: FakeQdrant) -> None:
    session = FakeSession(FakeRow("a-previous-digest"))

    result = await ingest(session, qdrant)

    assert result.status == "updated"


async def test_an_update_removes_the_old_points_first(qdrant: FakeQdrant) -> None:
    """Delete before upsert, so a superseded revision cannot survive the re-index."""
    session = FakeSession(FakeRow("a-previous-digest"))

    await ingest(session, qdrant)

    assert qdrant.calls == ["delete", "upsert"]
    assert qdrant.deleted == ["L33575"]


async def test_an_update_writes_the_new_chunks(qdrant: FakeQdrant) -> None:
    session = FakeSession(FakeRow("a-previous-digest"))

    result = await ingest(session, qdrant)

    assert result.chunks_indexed == 3


# --- ordering and failure --------------------------------------------------


async def test_qdrant_is_written_before_postgres(
    qdrant: FakeQdrant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering decides which way a partial failure fails — see the module docstring."""
    order: list[str] = []
    session = FakeSession()

    def upsert(passed: Any, name: str, points: Any) -> None:
        order.append("qdrant")

    monkeypatch.setattr(ingestion.vector_store, "upsert_points", upsert)

    async def record(*args: Any, **kwargs: Any) -> None:
        order.append("postgres")

    monkeypatch.setattr(ingestion, "_record_policy", record)

    await ingest(session, qdrant)

    assert order == ["qdrant", "postgres"]


async def test_a_pdf_that_will_not_parse_writes_nothing(
    qdrant: FakeQdrant, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession()

    def explode(data: bytes) -> str:
        raise PdfParseError("not a pdf")

    monkeypatch.setattr(ingestion, "extract_text", explode)

    with pytest.raises(PdfParseError):
        await ingest(session, qdrant)

    assert qdrant.calls == []
    assert session.commits == 0


async def test_a_pdf_with_no_text_is_refused(qdrant: FakeQdrant) -> None:
    """A scanned policy would otherwise record a hash with no vectors behind it."""
    session = FakeSession()

    with pytest.raises(EmptyDocumentError):
        await ingest(session, qdrant, pdf=b"")

    assert qdrant.calls == []
    assert session.commits == 0


async def test_an_empty_document_is_refused_before_any_delete(qdrant: FakeQdrant) -> None:
    """The existing index must survive an attempt to replace it with nothing."""
    session = FakeSession(FakeRow("a-previous-digest"))

    with pytest.raises(EmptyDocumentError):
        await ingest(session, qdrant, pdf=b"")

    assert qdrant.deleted == []

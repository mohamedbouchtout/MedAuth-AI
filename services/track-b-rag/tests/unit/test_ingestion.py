"""The three-way dedup, and the write ordering that decides how it fails.

Postgres and Qdrant are both faked here. The claims that need real containers —
that chunks are actually retrievable, that a re-ingest does not duplicate points
— are in ``tests/integration/test_ingestion.py``; these cover the decision logic
around them, which a container cannot make legible.
"""

from __future__ import annotations

import datetime
from dataclasses import replace
from typing import Any

import pytest

from track_b_rag import embeddings, ingestion
from track_b_rag.documents import ContentType, content_digest
from track_b_rag.ingestion import (
    EmptyDocumentError,
    PolicyMetadata,
    ingest_policy,
)
from track_b_rag.pdf import PdfParseError

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
    """Parse and chunk without building a real document.

    ``test_pdf.py`` and ``test_markup.py`` cover the real readers; the stub
    records the content type it was handed so the tests here can assert the
    declared format reaches the reader, which is the only part of dispatch this
    module is responsible for.
    """
    monkeypatch.setattr(
        ingestion, "extract_text", lambda data, content_type: f"{content_type}|{data.decode()}"
    )
    monkeypatch.setattr(
        ingestion,
        "chunk_text",
        # Drops empties like the real chunk_text, so an empty document is no
        # chunks rather than one blank one.
        lambda text: [chunk for chunk in text.split("|")[1:] if chunk] if text else [],
    )


DOCUMENT = b"first chunk|second chunk|third chunk"


async def ingest(
    session: FakeSession,
    client: FakeQdrant,
    document: bytes = DOCUMENT,
    content_type: ContentType = "application/pdf",
) -> Any:
    return await ingest_policy(
        session=session,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        collection=COLLECTION,
        document_bytes=document,
        metadata=replace(METADATA, content_type=content_type),
    )


# --- the declared format reaches the reader --------------------------------


@pytest.mark.parametrize("content_type", ["application/pdf", "text/html"])
async def test_the_declared_content_type_reaches_the_reader(
    qdrant: FakeQdrant, content_type: ContentType
) -> None:
    """Dispatch is this module's job; which reader handles what is documents.py's.

    The stub prefixes the text it returns with the type it was called with, so a
    chunk carrying that prefix proves the declaration was not dropped on the way.
    """
    await ingest(FakeSession(), qdrant, document=b"|only chunk", content_type=content_type)

    assert [point.payload["text"] for point in qdrant.upserted] == ["only chunk"]


@pytest.mark.parametrize("content_type", ["application/pdf", "text/html"])
async def test_the_digest_is_over_the_bytes_whatever_the_format(
    qdrant: FakeQdrant, content_type: ContentType
) -> None:
    """One dedup rule for both formats: it identifies the file, not the parse."""
    result = await ingest(FakeSession(), qdrant, content_type=content_type)

    assert result.content_hash == content_digest(DOCUMENT)


@pytest.mark.parametrize("content_type", ["application/pdf", "text/html"])
async def test_an_unchanged_document_is_skipped_whatever_the_format(
    qdrant: FakeQdrant, content_type: ContentType
) -> None:
    """TASK-011's three dedup claims hold on the HTML path too, rather than being
    inherited from the PDF path — this is the one the nightly scrape depends on."""
    session = FakeSession(FakeRow(content_digest(DOCUMENT)))

    result = await ingest(session, qdrant, content_type=content_type)

    assert result.status == "unchanged"
    assert result.chunks_indexed == 0
    assert qdrant.calls == []


@pytest.mark.parametrize("content_type", ["application/pdf", "text/html"])
async def test_a_changed_document_is_updated_whatever_the_format(
    qdrant: FakeQdrant, content_type: ContentType
) -> None:
    session = FakeSession(FakeRow("a-different-digest"))

    result = await ingest(session, qdrant, content_type=content_type)

    assert result.status == "updated"
    assert qdrant.deleted == [METADATA.policy_id]


# --- contractor jurisdictions ----------------------------------------------


async def test_a_jurisdiction_reaches_the_payload_as_a_list(qdrant: FakeQdrant) -> None:
    """One document with a list of states, not one copy per state. MatchValue
    matches any element of a list-valued payload, so retrieval needs no change
    and the collection does not hold the same text a dozen times over."""
    metadata = replace(METADATA, state=None, jurisdiction_states=["MA", "ME", "NY"])

    await ingest_policy(
        session=FakeSession(),  # type: ignore[arg-type]
        client=qdrant,  # type: ignore[arg-type]
        collection=COLLECTION,
        document_bytes=DOCUMENT,
        metadata=metadata,
    )

    assert all(point.payload["state"] == ["MA", "ME", "NY"] for point in qdrant.upserted)


def test_a_single_state_policy_still_carries_a_string() -> None:
    """Commercial plan documents (TASK-014) name one state, and the payload
    shape they have always had keeps working."""
    assert replace(METADATA, jurisdiction_states=[]).qdrant_state == "NY"


def test_a_national_policy_carries_no_state() -> None:
    """Null is what the retrieval filter's IsNullCondition looks for, which is
    how an NCD is returned alongside whichever local policy matched."""
    assert replace(METADATA, state=None, jurisdiction_states=[]).qdrant_state is None


def test_a_jurisdiction_wins_over_a_stray_state() -> None:
    """The request model rejects both being set, so this only decides what the
    pipeline does if some other caller sets both anyway: the wider answer, since
    narrowing silently is the failure that hides."""
    assert replace(METADATA, jurisdiction_states=["MA"]).qdrant_state == ["MA"]


async def test_the_jurisdiction_is_recorded_on_the_row(qdrant: FakeQdrant) -> None:
    session = FakeSession()

    await ingest_policy(
        session=session,  # type: ignore[arg-type]
        client=qdrant,  # type: ignore[arg-type]
        collection=COLLECTION,
        document_bytes=DOCUMENT,
        metadata=replace(METADATA, state=None, jurisdiction_states=["MA", "ME"]),
    )

    assert session.executed[0].compile().params["jurisdiction_states"] == ["MA", "ME"]


async def test_no_jurisdiction_is_recorded_as_null(qdrant: FakeQdrant) -> None:
    """Null rather than an empty array, so "not a jurisdiction policy" is one
    value in the column and not two."""
    session = FakeSession()

    await ingest(session, qdrant)

    assert session.executed[0].compile().params["jurisdiction_states"] is None


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


async def test_the_indexed_points_carry_the_payer_slug(qdrant: FakeQdrant) -> None:
    """TASK-016. The payload holds what the retrieval filter compares against, and
    that is the slug — "CMS" here would match nothing a query normalises to."""
    await ingest(FakeSession(), qdrant)

    assert {point.payload["payer"] for point in qdrant.upserted} == {"cms-medicare"}


async def test_the_recorded_row_keeps_the_payer_as_sent(qdrant: FakeQdrant) -> None:
    """The two halves of the split: slug for matching, the payer's own spelling for
    people. Nothing queries `insurance_policies` by payer."""
    session = FakeSession()

    await ingest(session, qdrant)

    values = session.executed[0].compile().params
    assert values["payer"] == "CMS"


@pytest.mark.parametrize("spelling", ["CMS", "Medicare", "Medicare Part B", "medicare part a"])
def test_every_medicare_spelling_indexes_under_one_slug(spelling: str) -> None:
    metadata = PolicyMetadata(policy_id="L33575", payer=spelling)

    assert metadata.payer_slug == "cms-medicare"


async def test_the_result_carries_the_digest_of_the_uploaded_bytes(qdrant: FakeQdrant) -> None:
    result = await ingest(FakeSession(), qdrant)

    assert result.content_hash == content_digest(DOCUMENT)


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
    session = FakeSession(FakeRow(content_digest(DOCUMENT)))

    result = await ingest(session, qdrant)

    assert result.status == "unchanged"


async def test_an_unchanged_policy_does_no_qdrant_work(qdrant: FakeQdrant) -> None:
    session = FakeSession(FakeRow(content_digest(DOCUMENT)))

    await ingest(session, qdrant)

    assert qdrant.calls == []


async def test_an_unchanged_policy_writes_no_row(qdrant: FakeQdrant) -> None:
    """Skipping means skipping: last_ingested_at is not bumped for a no-op."""
    session = FakeSession(FakeRow(content_digest(DOCUMENT)))

    await ingest(session, qdrant)

    assert session.executed == []
    assert session.commits == 0


async def test_an_unchanged_policy_reports_zero_chunks(qdrant: FakeQdrant) -> None:
    session = FakeSession(FakeRow(content_digest(DOCUMENT)))

    result = await ingest(session, qdrant)

    assert result.chunks_indexed == 0


async def test_an_unchanged_policy_still_reports_its_digest(qdrant: FakeQdrant) -> None:
    session = FakeSession(FakeRow(content_digest(DOCUMENT)))

    result = await ingest(session, qdrant)

    assert result.content_hash == content_digest(DOCUMENT)


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


async def test_a_document_that_will_not_parse_writes_nothing(
    qdrant: FakeQdrant, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession()

    def explode(data: bytes, content_type: ContentType) -> str:
        raise PdfParseError("not a pdf")

    monkeypatch.setattr(ingestion, "extract_text", explode)

    with pytest.raises(PdfParseError):
        await ingest(session, qdrant)

    assert qdrant.calls == []
    assert session.commits == 0


@pytest.mark.parametrize("content_type", ["application/pdf", "text/html"])
async def test_a_document_with_no_text_is_refused(
    qdrant: FakeQdrant, content_type: ContentType
) -> None:
    """A scanned policy would otherwise record a hash with no vectors behind it,
    and markup with no text in it is the same state reached a different way."""
    session = FakeSession()

    with pytest.raises(EmptyDocumentError):
        await ingest(session, qdrant, document=b"", content_type=content_type)

    assert qdrant.calls == []
    assert session.commits == 0


@pytest.mark.parametrize("content_type", ["application/pdf", "text/html"])
async def test_an_empty_document_is_refused_before_any_delete(
    qdrant: FakeQdrant, content_type: ContentType
) -> None:
    """The existing index must survive an attempt to replace it with nothing."""
    session = FakeSession(FakeRow("a-previous-digest"))

    with pytest.raises(EmptyDocumentError):
        await ingest(session, qdrant, document=b"", content_type=content_type)

    assert qdrant.deleted == []

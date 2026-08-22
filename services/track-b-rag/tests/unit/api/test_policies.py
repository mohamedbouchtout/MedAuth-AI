"""POST /policies/ingest — envelope, validation, error mapping, and what it does not do.

The ingestion pipeline itself is faked out here; ``tests/unit/test_ingestion.py``
covers the dedup decisions and ``tests/integration/test_ingestion.py`` covers the
real stores. What is left for this module is the HTTP contract: the envelope, the
multipart parsing, which failures become which status, and the standing decision
that this route writes no audit row.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from track_b_rag.api import policies
from track_b_rag.api.dependencies import get_db_session, get_qdrant
from track_b_rag.config import get_settings
from track_b_rag.ingestion import EmptyDocumentError, IngestResult, PolicyMetadata
from track_b_rag.main import create_app
from track_b_rag.markup import HtmlParseError
from track_b_rag.pdf import PdfParseError

PDF = b"%PDF-1.4 a policy document"
HTML = b"<p>a policy document</p>"


class Recorder:
    """Captures the metadata the route assembled, and decides what to raise."""

    def __init__(self) -> None:
        self.metadata: PolicyMetadata | None = None
        self.document_bytes: bytes | None = None
        self.collection: str | None = None
        self.error: Exception | None = None
        self.result = IngestResult(
            policy_id="L33575",
            status="created",
            content_hash="a" * 64,
            chunks_indexed=7,
            collection="insurance_policies",
        )


@pytest.fixture(autouse=True)
def clean_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    captured = Recorder()

    async def fake_ingest(
        *,
        session: Any,
        client: Any,
        collection: str,
        document_bytes: bytes,
        metadata: PolicyMetadata,
    ) -> IngestResult:
        captured.metadata = metadata
        captured.document_bytes = document_bytes
        captured.collection = collection
        if captured.error is not None:
            raise captured.error
        return captured.result

    monkeypatch.setattr(policies, "ingest_policy", fake_ingest)
    return captured


@pytest_asyncio.fixture
async def client(recorder: Recorder) -> AsyncIterator[AsyncClient]:
    """An HTTP client with both backing dependencies replaced by inert stand-ins."""
    app = create_app()
    app.dependency_overrides[get_qdrant] = lambda: object()
    app.dependency_overrides[get_db_session] = lambda: object()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://track-b-rag") as http:
        yield http


def form(**overrides: str) -> dict[str, str]:
    fields = {"policy_id": "L33575", "payer": "CMS"}
    fields.update(overrides)
    return fields


def upload(document: bytes = PDF) -> dict[str, tuple[str, bytes, str]]:
    """The multipart part itself. What the route dispatches on is the
    `content_type` form field, not this — a multipart part's own type is set by
    whatever client library sent it and is not a declaration by the caller."""
    return {"file": ("policy.pdf", document, "application/pdf")}


# --- the success contract --------------------------------------------------


async def test_an_ingest_returns_the_standard_envelope(client: AsyncClient) -> None:
    response = await client.post("/policies/ingest", data=form(), files=upload())

    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "policy_id": "L33575",
            "status": "created",
            "content_hash": "a" * 64,
            "chunks_indexed": 7,
            "collection": "insurance_policies",
        },
        "error": None,
    }


@pytest.mark.parametrize("status_value", ["created", "updated", "unchanged"])
async def test_every_dedup_outcome_is_a_200(
    client: AsyncClient, recorder: Recorder, status_value: str
) -> None:
    """All three are successful ingests; the status field is what distinguishes them."""
    recorder.result = IngestResult(
        policy_id="L33575",
        status=status_value,  # type: ignore[arg-type]
        content_hash="b" * 64,
        chunks_indexed=0,
        collection="insurance_policies",
    )

    response = await client.post("/policies/ingest", data=form(), files=upload())

    assert response.status_code == 200
    assert response.json()["data"]["status"] == status_value


async def test_the_uploaded_bytes_reach_the_pipeline(
    client: AsyncClient, recorder: Recorder
) -> None:
    await client.post("/policies/ingest", data=form(), files=upload(b"%PDF-1.7 specific"))

    assert recorder.document_bytes == b"%PDF-1.7 specific"


async def test_the_configured_collection_is_used(
    client: AsyncClient, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QDRANT_COLLECTION", "policies_staging")
    get_settings.cache_clear()

    await client.post("/policies/ingest", data=form(), files=upload())

    assert recorder.collection == "policies_staging"


# --- metadata parsing ------------------------------------------------------


async def test_every_metadata_field_is_forwarded(client: AsyncClient, recorder: Recorder) -> None:
    await client.post(
        "/policies/ingest",
        data=form(
            payer="Aetna",
            plan_type="PPO",
            state="NY",
            source_url="https://example.com/policy.pdf",
            effective_date="2026-01-01",
        ),
        files=upload(),
    )

    assert recorder.metadata == PolicyMetadata(
        policy_id="L33575",
        payer="Aetna",
        plan_type="PPO",
        state="NY",
        source_url="https://example.com/policy.pdf",
        effective_date=datetime.date(2026, 1, 1),
    )


async def test_optional_fields_default_to_none(client: AsyncClient, recorder: Recorder) -> None:
    await client.post("/policies/ingest", data=form(), files=upload())

    assert recorder.metadata is not None
    assert recorder.metadata.plan_type is None
    assert recorder.metadata.state is None
    assert recorder.metadata.source_url is None
    assert recorder.metadata.effective_date is None


async def test_an_empty_optional_field_is_treated_as_absent(
    client: AsyncClient, recorder: Recorder
) -> None:
    """A multipart client renders an unset field as name="" — that means no value."""
    await client.post(
        "/policies/ingest",
        data=form(plan_type="", state="", source_url="", effective_date=""),
        files=upload(),
    )

    assert recorder.metadata is not None
    assert recorder.metadata.plan_type is None
    assert recorder.metadata.state is None


async def test_a_state_code_is_normalised_to_uppercase(
    client: AsyncClient, recorder: Recorder
) -> None:
    """The column is CHAR(2), and TASK-012 filters on the payload value."""
    await client.post("/policies/ingest", data=form(state="ny"), files=upload())

    assert recorder.metadata is not None
    assert recorder.metadata.state == "NY"


@pytest.mark.parametrize("state", ["N", "NYC", "12"])
async def test_a_malformed_state_code_is_rejected(client: AsyncClient, state: str) -> None:
    response = await client.post("/policies/ingest", data=form(state=state), files=upload())

    assert response.status_code == 422


@pytest.mark.parametrize("missing", ["policy_id", "payer"])
async def test_the_required_metadata_is_required(client: AsyncClient, missing: str) -> None:
    fields = form()
    del fields[missing]

    response = await client.post("/policies/ingest", data=fields, files=upload())

    assert response.status_code == 422


async def test_the_file_is_required(client: AsyncClient) -> None:
    response = await client.post("/policies/ingest", data=form())

    assert response.status_code == 422


async def test_an_unknown_metadata_field_is_rejected(client: AsyncClient) -> None:
    """extra="forbid" — a typo'd field name should fail, not be silently dropped."""
    response = await client.post("/policies/ingest", data=form(polcy_id="typo"), files=upload())

    assert response.status_code == 422


async def test_a_validation_failure_uses_the_envelope(client: AsyncClient) -> None:
    response = await client.post("/policies/ingest", data=form(state="XYZ"), files=upload())

    body = response.json()
    assert body["data"] is None
    assert body["error"]["code"] == "validation_error"


async def test_a_validation_failure_never_echoes_the_rejected_value(
    client: AsyncClient,
) -> None:
    """The api-envelope handler reports field locations only — a HIPAA constraint."""
    response = await client.post("/policies/ingest", data=form(state="SECRET"), files=upload())

    assert "SECRET" not in response.text


# --- the declared content type ---------------------------------------------


async def test_the_content_type_defaults_to_pdf(client: AsyncClient, recorder: Recorder) -> None:
    """Payers publish PDFs, so a caller that says nothing means PDF. This is also
    what keeps TASK-014's seed script working unchanged."""
    await client.post("/policies/ingest", data=form(), files=upload())

    assert recorder.metadata is not None
    assert recorder.metadata.content_type == "application/pdf"


async def test_html_can_be_declared(client: AsyncClient, recorder: Recorder) -> None:
    """CMS publishes no PDF at all, so TASK-013 has HTML and nothing else."""
    await client.post(
        "/policies/ingest",
        data=form(content_type="text/html"),
        files=upload(HTML),
    )

    assert recorder.metadata is not None
    assert recorder.metadata.content_type == "text/html"
    assert recorder.document_bytes == HTML


@pytest.mark.parametrize("declared", ["application/xml", "text/plain", "pdf", ""])
async def test_an_unsupported_content_type_is_rejected(
    client: AsyncClient, recorder: Recorder, declared: str
) -> None:
    """A format with no reader must not fall through to the PDF one and fail
    later as an unreadable document."""
    response = await client.post(
        "/policies/ingest",
        data=form(content_type=declared),
        files=upload(),
    )

    assert response.status_code == 422
    assert recorder.metadata is None


async def test_the_rejected_content_type_is_not_echoed(
    client: AsyncClient, recorder: Recorder
) -> None:
    """api-envelope reports field locations only, and that holds for this field
    like every other one."""
    response = await client.post(
        "/policies/ingest",
        data=form(content_type="application/secret-format"),
        files=upload(),
    )

    assert "secret-format" not in response.text


# --- error mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        PdfParseError("The uploaded file could not be read as a PDF."),
        HtmlParseError("The uploaded file could not be decoded as text."),
    ],
    ids=["pdf", "html"],
)
async def test_an_unreadable_document_is_a_400(
    client: AsyncClient, recorder: Recorder, error: Exception
) -> None:
    """The route catches DocumentParseError, so a new format's reader answers 400
    without the route learning about it."""
    recorder.error = error

    response = await client.post("/policies/ingest", data=form(), files=upload(b"nope"))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_document"


async def test_a_document_with_no_text_is_a_400(client: AsyncClient, recorder: Recorder) -> None:
    recorder.error = EmptyDocumentError("no extractable text")

    response = await client.post("/policies/ingest", data=form(), files=upload())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_document"


async def test_the_two_400s_are_distinguishable(client: AsyncClient, recorder: Recorder) -> None:
    """A scraper author needs to know whether to fix the fetch or the document."""
    recorder.error = PdfParseError("unreadable")
    unreadable = await client.post("/policies/ingest", data=form(), files=upload())
    recorder.error = EmptyDocumentError("empty")
    empty = await client.post("/policies/ingest", data=form(), files=upload())

    assert unreadable.json()["error"]["code"] != empty.json()["error"]["code"]


async def test_an_error_carries_no_data(client: AsyncClient, recorder: Recorder) -> None:
    recorder.error = PdfParseError("unreadable")

    response = await client.post("/policies/ingest", data=form(), files=upload())

    assert response.json()["data"] is None


# --- the standing decisions ------------------------------------------------


async def test_the_route_writes_no_audit_row(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known Constraints #6: audit if and only if the route touches PHI. This does not.

    Policy documents are public payer publications with no patient linkage, and
    mixing operational writes into audit_log turns "who accessed patient X" into
    a query you have to filter rather than one you can just run.
    """
    calls: list[object] = []

    import hipaa_logger

    monkeypatch.setattr(
        hipaa_logger, "audit_log", lambda *args, **kwargs: calls.append(kwargs), raising=False
    )

    response = await client.post("/policies/ingest", data=form(), files=upload())

    assert response.status_code == 200
    assert calls == []


def test_the_route_module_has_no_audit_call_site() -> None:
    """A guard against a later edit reintroducing one, ignoring prose about it.

    Checks executable code rather than the file text — the module docstring
    explains at length *why* there is no audit call, and a substring search over
    the source would flag its own explanation.
    """
    import ast

    with open(policies.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }

    assert "audit_log" not in called

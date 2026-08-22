"""What this service sends to /policies/ingest, and what it does with the answer.

The endpoint owns chunking, embedding and the dedup decision. What is testable
here is that the upload says what it means — the right payer slug, the right
content type, the jurisdiction as repeated fields — and that a rejection is not
quietly counted as an ingest.
"""

from __future__ import annotations

import datetime

import httpx
import pytest

from policy_scraper.documents import PolicyDocument
from policy_scraper.ingest import IngestFailed, upload

BASE = "http://track-b-rag:8002"

DOCUMENT = PolicyDocument(
    policy_id="cms-lcd-L39529",
    title="Intraarticular Knee Injections of Hyaluronan",
    body=b"<p>Conservative therapy for six weeks.</p>",
    states=["MA", "ME", "NY"],
    source_url="https://www.cms.gov/medicare-coverage-database/view/lcd.aspx?lcdid=39529",
    effective_date=datetime.date(2025, 5, 1),
)

OK_BODY = {
    "data": {
        "policy_id": "cms-lcd-L39529",
        "status": "created",
        "content_hash": "a" * 64,
        "chunks_indexed": 12,
        "collection": "insurance_policies",
    },
    "error": None,
}


class Capture:
    """Records the multipart request and answers with a scripted response."""

    def __init__(self, status: int = 200, body: object = OK_BODY) -> None:
        self.request: httpx.Request | None = None
        self.status = status
        self.body = body

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return httpx.Response(self.status, json=self.body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    @property
    def text(self) -> str:
        assert self.request is not None
        return self.request.content.decode("utf-8", errors="replace")


async def send(capture: Capture, document: PolicyDocument = DOCUMENT) -> dict[str, object]:
    async with capture.client() as client:
        return await upload(client, base_url=BASE, document=document)


class TestWhatIsSent:
    async def test_it_posts_to_the_ingest_endpoint(self) -> None:
        capture = Capture()

        await send(capture)

        assert capture.request is not None
        assert str(capture.request.url) == f"{BASE}/policies/ingest"

    async def test_the_payer_is_the_canonical_slug(self) -> None:
        """Not "CMS": the retrieval filter matches the payer by exact equality,
        against a slug a FHIR Coverage display name normalises to."""
        capture = Capture()

        await send(capture)

        assert 'name="payer"\r\n\r\ncms-medicare' in capture.text

    async def test_the_content_type_declares_html(self) -> None:
        """CMS publishes no PDF, and the digest covers the bytes it does
        publish."""
        capture = Capture()

        await send(capture)

        assert 'name="content_type"\r\n\r\ntext/html' in capture.text

    async def test_the_jurisdiction_is_sent_as_repeated_fields(self) -> None:
        capture = Capture()

        await send(capture)

        assert capture.text.count('name="jurisdiction_states"') == 3

    async def test_a_national_policy_sends_no_jurisdiction(self) -> None:
        """An NCD applies everywhere, which is stored as no state at all."""
        capture = Capture()

        await send(capture, PolicyDocument(**{**vars(DOCUMENT), "states": []}))

        assert 'name="jurisdiction_states"' not in capture.text

    async def test_the_effective_date_is_iso_formatted(self) -> None:
        capture = Capture()

        await send(capture)

        assert 'name="effective_date"\r\n\r\n2025-05-01' in capture.text

    async def test_a_document_with_no_date_omits_the_field(self) -> None:
        """An empty form field is not the same as an absent one, and the request
        model treats blank as absent only because it was made to."""
        capture = Capture()

        await send(capture, PolicyDocument(**{**vars(DOCUMENT), "effective_date": None}))

        assert 'name="effective_date"' not in capture.text

    async def test_the_body_is_uploaded_as_the_file(self) -> None:
        capture = Capture()

        await send(capture)

        assert "Conservative therapy for six weeks" in capture.text

    async def test_the_source_url_records_provenance(self) -> None:
        capture = Capture()

        await send(capture)

        assert "view/lcd.aspx?lcdid=39529" in capture.text


class TestTheAnswer:
    async def test_the_status_comes_back(self) -> None:
        assert (await send(Capture()))["status"] == "created"

    async def test_an_error_status_raises(self) -> None:
        """A rejected document counted as ingested would report a successful
        scrape while leaving the collection short a policy."""
        with pytest.raises(IngestFailed, match="HTTP 400"):
            await send(Capture(status=400, body={"data": None, "error": {"code": "invalid_pdf"}}))

    async def test_the_failure_names_the_document(self) -> None:
        with pytest.raises(IngestFailed, match="cms-lcd-L39529"):
            await send(Capture(status=500, body={"data": None, "error": {}}))

    @pytest.mark.parametrize(
        "body",
        [{"data": None, "error": None}, {"data": {"policy_id": "x"}, "error": None}, {}],
    )
    async def test_an_unrecognised_body_raises(self, body: object) -> None:
        """A 200 whose shape we do not recognise means the envelope changed
        under us, which is not a document that ingested fine."""
        with pytest.raises(IngestFailed, match="unrecognised body"):
            await send(Capture(body=body))

"""A whole run, with CMS and the ingest endpoint faked out.

This is where the pieces meet: three archives in, a filtered set of documents
out, and only the ones whose digest the database does not already hold uploaded.
The database read is stubbed rather than mocked at the driver level — what
matters is which documents get skipped, not how the row was fetched.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from policy_scraper import mcd, scrape
from policy_scraper.config import Settings
from policy_scraper.fetch import PoliteClient
from tests.fixtures import article_export, lcd_export, ncd_export

SETTINGS = Settings(
    policy_scraper_user_agent="MedAuthAI-PolicyScraper/0.1 (scraper@medauth.ai)",
    database_url="postgresql+asyncpg://ignored/ignored",
    track_b_rag_url="http://track-b-rag:8002",
    request_delay_seconds=0.0,
)

EXPORTS = {
    mcd.LCD_EXPORT: lcd_export,
    mcd.ARTICLE_EXPORT: article_export,
    mcd.NCD_EXPORT: ncd_export,
}


def cms_transport() -> httpx.MockTransport:
    """Serves robots.txt and the three archives, and nothing else."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        for name, build in EXPORTS.items():
            if request.url.path.endswith(name):
                return httpx.Response(200, content=build())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def client() -> PoliteClient:
    return PoliteClient(
        user_agent=SETTINGS.policy_scraper_user_agent,
        delay_seconds=0.0,
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=cms_transport()),
    )


class TestCollecting:
    async def test_only_targeted_lcds_are_collected(self, client: PoliteClient) -> None:
        """The wheelchair LCD is reachable in the export and must not be
        collected — that is what scoping the scrape means."""
        documents = await scrape.collect_documents(SETTINGS, client)

        ids = {document.policy_id for document in documents}
        assert "cms-lcd-L39529" in ids
        assert "cms-lcd-L34220" in ids
        assert "cms-lcd-L33312" not in ids

    async def test_national_determinations_are_collected(self, client: PoliteClient) -> None:
        documents = await scrape.collect_documents(SETTINGS, client)

        assert "cms-ncd-220.2" in {document.policy_id for document in documents}

    async def test_an_empty_determination_is_not_collected(self, client: PoliteClient) -> None:
        documents = await scrape.collect_documents(SETTINGS, client)

        assert "cms-ncd-999.9" not in {document.policy_id for document in documents}

    async def test_a_local_policy_carries_its_jurisdiction(self, client: PoliteClient) -> None:
        documents = await scrape.collect_documents(SETTINGS, client)

        lcd = next(d for d in documents if d.policy_id == "cms-lcd-L39529")
        assert lcd.states == ["MA", "NY"]

    async def test_a_national_policy_carries_no_states(self, client: PoliteClient) -> None:
        documents = await scrape.collect_documents(SETTINGS, client)

        ncd = next(d for d in documents if d.policy_id == "cms-ncd-220.2")
        assert ncd.states == []

    async def test_every_archive_is_fetched_once(self, client: PoliteClient) -> None:
        """Three requests plus one robots.txt per host. No per-document fetches:
        the export carries the policy text."""
        requested: list[str] = []

        original = client.get

        async def record(url: str) -> bytes:
            requested.append(url)
            return await original(url)

        client.get = record  # type: ignore[method-assign]
        await scrape.collect_documents(SETTINGS, client)

        assert len(requested) == 3


class TestUploading:
    @pytest.fixture
    def uploads(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        sent: list[str] = []

        async def fake_upload(
            client: httpx.AsyncClient, *, base_url: str, document: Any
        ) -> dict[str, Any]:
            sent.append(document.policy_id)
            return {"status": "created", "chunks_indexed": 3}

        monkeypatch.setattr(scrape, "upload", fake_upload)
        return sent

    def stub_store(self, monkeypatch: pytest.MonkeyPatch, stored: dict[str, str]) -> None:
        class FakeSession:
            async def __aenter__(self) -> FakeSession:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

        monkeypatch.setattr(scrape, "session_factory", lambda url: FakeSession)

        async def fake_hashes(session: object, policy_ids: set[str]) -> dict[str, str]:
            return {key: value for key, value in stored.items() if key in policy_ids}

        monkeypatch.setattr(scrape, "known_content_hashes", fake_hashes)

    async def run_upload(
        self, monkeypatch: pytest.MonkeyPatch, client: PoliteClient, stored: dict[str, str]
    ) -> scrape.ScrapeSummary:
        documents = await scrape.collect_documents(SETTINGS, client)
        self.stub_store(monkeypatch, stored)
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as http:
            return await scrape.upload_documents(documents, settings=SETTINGS, ingest_client=http)

    async def test_everything_is_uploaded_on_a_first_run(
        self, client: PoliteClient, monkeypatch: pytest.MonkeyPatch, uploads: list[str]
    ) -> None:
        summary = await self.run_upload(monkeypatch, client, stored={})

        assert summary.uploaded == summary.considered
        assert summary.skipped_unchanged == 0

    async def test_a_document_with_a_matching_digest_is_not_uploaded(
        self, client: PoliteClient, monkeypatch: pytest.MonkeyPatch, uploads: list[str]
    ) -> None:
        """The bandwidth optimisation: most nights, most policies have not
        changed and there is no reason to send them again."""
        documents = await scrape.collect_documents(SETTINGS, client)
        unchanged = next(d for d in documents if d.policy_id == "cms-lcd-L39529")

        summary = await self.run_upload(
            monkeypatch, client, stored={unchanged.policy_id: unchanged.content_hash}
        )

        assert "cms-lcd-L39529" not in uploads
        assert summary.skipped_unchanged == 1

    async def test_a_document_with_a_different_digest_is_uploaded(
        self, client: PoliteClient, monkeypatch: pytest.MonkeyPatch, uploads: list[str]
    ) -> None:
        summary = await self.run_upload(
            monkeypatch, client, stored={"cms-lcd-L39529": "a-stale-digest"}
        )

        assert "cms-lcd-L39529" in uploads
        assert summary.skipped_unchanged == 0

    async def test_one_failing_document_does_not_stop_the_run(
        self, client: PoliteClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A policy that cannot be ingested should cost that policy, not the
        other forty."""
        attempted: list[str] = []

        async def flaky(
            client: httpx.AsyncClient, *, base_url: str, document: Any
        ) -> dict[str, Any]:
            attempted.append(document.policy_id)
            if document.policy_id == "cms-lcd-L39529":
                raise RuntimeError("ingest said no")
            return {"status": "created"}

        monkeypatch.setattr(scrape, "upload", flaky)
        summary = await self.run_upload(monkeypatch, client, stored={})

        assert summary.failed == 1
        assert len(attempted) == summary.considered

    async def test_the_summary_counts_each_status(
        self, client: PoliteClient, monkeypatch: pytest.MonkeyPatch, uploads: list[str]
    ) -> None:
        summary = await self.run_upload(monkeypatch, client, stored={})

        assert summary.statuses == {"created": summary.uploaded}

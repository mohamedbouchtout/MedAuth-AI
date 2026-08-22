"""The CronJob entry point, and the run it wraps.

What matters here is the exit code. Kubernetes decides whether a nightly job
succeeded from it, and a run that indexed nothing while exiting zero is exactly
the silent failure this service is built to avoid.
"""

from __future__ import annotations

import httpx
import pytest

from policy_scraper import __main__ as entrypoint
from policy_scraper import scrape
from policy_scraper.config import Settings, get_settings
from policy_scraper.scrape import ScrapeSummary

SETTINGS = Settings(
    policy_scraper_user_agent="MedAuthAI-PolicyScraper/0.1 (scraper@medauth.ai)",
    database_url="postgresql+asyncpg://ignored/ignored",
    request_delay_seconds=0.0,
)


@pytest.fixture(autouse=True)
def stub_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, "get_settings", lambda: SETTINGS)


def stub_run(monkeypatch: pytest.MonkeyPatch, summary: ScrapeSummary | Exception) -> None:
    async def fake_run(settings: Settings) -> ScrapeSummary:
        if isinstance(summary, Exception):
            raise summary
        return summary

    monkeypatch.setattr(entrypoint, "run", fake_run)


def test_a_clean_run_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_run(monkeypatch, ScrapeSummary(considered=3, uploaded=3))

    assert entrypoint.main() == 0


def test_a_run_with_a_failed_document_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """One policy that could not be ingested is a job a human should look at,
    even though the other forty went in."""
    stub_run(monkeypatch, ScrapeSummary(considered=3, uploaded=2, failed=1))

    assert entrypoint.main() == 1


def test_a_crashed_run_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_run(monkeypatch, RuntimeError("CMS changed the export layout"))

    assert entrypoint.main() == 1


def test_a_crash_is_logged_with_its_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The traceback is the point: whoever reads the log tomorrow morning needs
    to know why, not just that."""
    stub_run(monkeypatch, RuntimeError("CMS changed the export layout"))

    with caplog.at_level("ERROR"):
        entrypoint.main()

    assert "CMS changed the export layout" in caplog.text


def test_a_run_that_uploaded_nothing_because_nothing_changed_is_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Most nights, every policy is unchanged. That is the system working."""
    stub_run(monkeypatch, ScrapeSummary(considered=40, skipped_unchanged=40))

    assert entrypoint.main() == 0


def test_settings_are_read_once() -> None:
    get_settings.cache_clear()
    try:
        first = get_settings.cache_info()
        assert first.currsize == 0
    finally:
        get_settings.cache_clear()


class TestRun:
    async def test_it_collects_then_uploads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The two halves in order, with one HTTP client for CMS and another for
        the internal service — different timeouts, different hosts."""
        calls: list[str] = []

        async def fake_collect(settings: Settings, client: object) -> list[object]:
            calls.append("collect")
            return ["document"]

        async def fake_upload(
            documents: list[object], *, settings: Settings, ingest_client: httpx.AsyncClient
        ) -> ScrapeSummary:
            calls.append("upload")
            assert documents == ["document"]
            return ScrapeSummary(considered=1, uploaded=1)

        monkeypatch.setattr(scrape, "collect_documents", fake_collect)
        monkeypatch.setattr(scrape, "upload_documents", fake_upload)

        summary = await scrape.run(SETTINGS)

        assert calls == ["collect", "upload"]
        assert summary.uploaded == 1

    async def test_the_summary_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """This service writes no audit row — coverage determinations are public
        payer publications — so the INFO log is the operational record."""

        async def fake_collect(settings: Settings, client: object) -> list[object]:
            return []

        async def fake_upload(
            documents: list[object], *, settings: Settings, ingest_client: httpx.AsyncClient
        ) -> ScrapeSummary:
            return ScrapeSummary(considered=2, skipped_unchanged=1, uploaded=1)

        monkeypatch.setattr(scrape, "collect_documents", fake_collect)
        monkeypatch.setattr(scrape, "upload_documents", fake_upload)

        with caplog.at_level("INFO", logger="policy_scraper.scrape"):
            await scrape.run(SETTINGS)

        assert "Scrape complete" in caplog.text

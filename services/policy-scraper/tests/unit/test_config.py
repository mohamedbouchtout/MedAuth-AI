"""Settings, and the two things they refuse to start without."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from policy_scraper.config import Settings, get_settings

REQUIRED = {
    "policy_scraper_user_agent": "MedAuthAI-PolicyScraper/0.1 (scraper@medauth.ai)",
    "database_url": "postgresql+asyncpg://user:pass@host/db",
}


def settings(**overrides: object) -> Settings:
    return Settings(**{**REQUIRED, **overrides})  # type: ignore[arg-type]


def test_get_settings_reads_the_environment_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """One read per process: the CronJob is a one-shot run and re-reading the
    environment mid-run could only produce two answers to the same question."""
    monkeypatch.setenv("POLICY_SCRAPER_USER_AGENT", REQUIRED["policy_scraper_user_agent"])
    monkeypatch.setenv("DATABASE_URL", REQUIRED["database_url"])
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


def test_the_user_agent_must_carry_a_contact_address() -> None:
    """TASK-013 asks for a User-Agent CMS can reach a human through. A default
    that quietly omitted it would be the value that shipped."""
    with pytest.raises(ValidationError, match="contact email"):
        settings(policy_scraper_user_agent="MedAuthAI-PolicyScraper/0.1")


def test_a_user_agent_with_a_contact_is_accepted() -> None:
    assert "@" in settings().policy_scraper_user_agent


def test_the_two_cms_hosts_are_separate_settings() -> None:
    """robots.txt is per host, and the exports are not on the database's host —
    conflating them would check the wrong file."""
    config = settings()

    assert "downloads.cms.gov" in config.cms_mcd_exports_base_url
    assert "www.cms.gov" in config.cms_coverage_db_base_url


@pytest.mark.parametrize(
    "field",
    ["cms_mcd_exports_base_url", "cms_coverage_db_base_url", "track_b_rag_url"],
)
def test_a_trailing_slash_is_stripped(field: str) -> None:
    """So joins produce one slash rather than two."""
    config = settings(**{field: "https://example.test/base/"})

    assert getattr(config, field) == "https://example.test/base"


def test_the_delay_may_be_zero_but_not_negative() -> None:
    assert settings(request_delay_seconds=0).request_delay_seconds == 0
    with pytest.raises(ValidationError):
        settings(request_delay_seconds=-1)


def test_the_download_timeout_is_sized_for_an_archive_not_an_api_call() -> None:
    """The LCD export is ~32 MB and the article export ~41 MB."""
    assert settings().download_timeout_seconds >= 300

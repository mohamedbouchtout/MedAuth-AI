"""Settings for the nightly scrape.

Nothing here has a value that differs per policy or per run. The two CMS hosts
are separate settings because they *are* separate hosts — the database UI is
``www.cms.gov`` and the bulk exports are served from ``downloads.cms.gov`` — and
robots.txt is per-host, so conflating them would check the wrong file.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration read from the environment."""

    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")

    #: Where the daily CSV exports live. A different host from the database UI.
    cms_mcd_exports_base_url: str = Field(
        default="https://downloads.cms.gov/medicare-coverage-database/downloads/exports/",
    )
    #: The database UI, used for the provenance URL recorded against each policy
    #: and for the robots.txt this scraper honours.
    cms_coverage_db_base_url: str = Field(
        default="https://www.cms.gov/medicare-coverage-database/",
    )
    #: Identifies this scraper to CMS, with a contact address. Load-bearing:
    #: cms.gov answers 403 to some clients based on their User-Agent.
    policy_scraper_user_agent: str = Field(min_length=1)
    #: Where to POST documents. The ingest endpoint owns all chunking and
    #: embedding; this service never writes Qdrant itself.
    track_b_rag_url: str = Field(default="http://localhost:8002")
    database_url: str = Field(min_length=1)

    #: Politeness, not a published requirement — CMS's robots.txt sets no
    #: Crawl-delay. Kept anyway: this is a government service and a nightly job
    #: has no reason to hurry.
    request_delay_seconds: float = Field(default=1.5, ge=0)
    #: The LCD export is ~32 MB and the article export ~41 MB, so the timeout is
    #: sized for a slow transfer rather than for an API call.
    download_timeout_seconds: float = Field(default=600.0, gt=0)
    ingest_timeout_seconds: float = Field(default=120.0, gt=0)

    @field_validator("cms_mcd_exports_base_url", "cms_coverage_db_base_url", "track_b_rag_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """Normalise so joins produce one slash rather than two or none."""
        return value.rstrip("/")

    @field_validator("policy_scraper_user_agent")
    @classmethod
    def _must_carry_a_contact(cls, value: str) -> str:
        """Require a contact address in the User-Agent.

        TASK-013 asks for a User-Agent identifying MedAuth AI *with a contact
        email*, because the courtesy of a scraper that a site owner can reach is
        the whole point. A default that quietly omitted it would be the value
        that shipped, so this is a validation error rather than a docstring.
        """
        if "@" not in value:
            raise ValueError(
                "POLICY_SCRAPER_USER_AGENT must include a contact email address, "
                "so CMS can reach a human about this scraper."
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read once."""
    return Settings()  # type: ignore[call-arg]

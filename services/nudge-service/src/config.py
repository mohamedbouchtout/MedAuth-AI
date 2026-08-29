"""Runtime configuration for the nudge-service.

Values come from the process environment only, for the reasons given in
``track_a_clinical.config``: local development exports them from ``.env.local``,
CI sets them on the job, deployments inject them from AWS Secrets Manager, and
reading a file here would add a fourth source of truth and a tempting place to
commit a secret.

``jwt_signing_key`` carries the 32-byte floor from ``session_auth``, the same one
the issuer and the audio socket enforce. This service validates tokens it did not
mint, so accepting a key the issuer refuses would turn a configuration mistake
into connections that are rejected for no visible reason.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from cors_policy import AllowedOrigins
from session_auth import MIN_SIGNING_KEY_BYTES


class Settings(BaseSettings):
    """Environment-backed settings for the nudge relay."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    jwt_signing_key: str = Field(min_length=MIN_SIGNING_KEY_BYTES)
    redis_url: str = Field(default="redis://localhost:6379/0", min_length=1)
    #: Browser origins this service answers, from ``CORS_ALLOWED_ORIGINS``.
    #: Empty by default, so an unconfigured deployment answers no browser rather
    #: than trusting one nobody chose — a localhost origin baked in as a default
    #: would ship to production the moment the variable was forgotten. Local dev
    #: gets its value from ``.env.example``. See CLAUDE.md, "CORS and browser
    #: reachability".
    cors_allowed_origins: AllowedOrigins = ()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment once.

    Tests that change the environment must call ``get_settings.cache_clear()``.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment

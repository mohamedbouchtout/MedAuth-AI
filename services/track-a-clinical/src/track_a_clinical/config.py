"""Runtime configuration for the track-a-clinical service.

Values come from the process environment only — no ``.env`` file is read here.
Local development exports them from ``.env.local`` (see CLAUDE.md), CI sets them
on the job, and deployments inject them from AWS Secrets Manager. Reading a file
from inside the service would give a fourth source of truth and a tempting place
to commit a secret.

The settings object is built lazily and cached, so a missing ``JWT_SIGNING_KEY``
surfaces on the first request that needs it rather than at import time — which
would otherwise make the module unimportable in tests that never mint a token.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 15 minutes, the session-JWT lifetime fixed in CLAUDE.md "Session Lifecycle &
#: JWT Issuance". Overridable through ``SESSION_TTL_SECONDS`` so the value is not
#: hardcoded at the call site.
DEFAULT_SESSION_TTL_SECONDS: Final = 900

#: HS256 keys shorter than the hash output weaken the MAC, and PyJWT warns about
#: them. Enforced here so a placeholder secret cannot reach a running service.
MIN_SIGNING_KEY_BYTES: Final = 32

#: Symmetric signing is deliberate for v1 — every service that validates these
#: tokens is first-party and already shares the secret.
JWT_ALGORITHM: Final = "HS256"


class Settings(BaseSettings):
    """Environment-backed settings for session lifecycle.

    ``JWT_ISSUER`` and ``JWT_AUDIENCE`` exist in ``.env.example`` but are
    deliberately absent here: v1 tokens carry ``{session_id, provider_id, exp}``
    and nothing else, because TASK-020's and TASK-041's validators would have to
    grow to match any claim added here.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    jwt_signing_key: str = Field(min_length=MIN_SIGNING_KEY_BYTES)
    session_ttl_seconds: int = Field(default=DEFAULT_SESSION_TTL_SECONDS, gt=0)
    redis_url: str = Field(default="redis://localhost:6379/0", min_length=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment once.

    Tests that change the environment must call ``get_settings.cache_clear()``.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment

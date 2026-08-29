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

from cors_policy import AllowedOrigins

#: 15 minutes, the session-JWT lifetime fixed in CLAUDE.md "Session Lifecycle &
#: JWT Issuance". Overridable through ``SESSION_TTL_SECONDS`` so the value is not
#: hardcoded at the call site.
DEFAULT_SESSION_TTL_SECONDS: Final = 900

#: How far past ``exp`` a token may still be presented as the credential for a
#: re-mint (TASK-006b).
#:
#: **This number is an assumption, not a measurement.** It was chosen when
#: TASK-006b was built, accepted deliberately as a starting value rather than
#: derived from any observed client behaviour, and it has not been validated
#: against a real visit since. Treat it as provisional: if it turns out to be
#: wrong it is wrong in a direction that matters, since it is what bounds how
#: long a captured token stays useful.
#:
#: What brackets it, as opposed to justifying it: it has to comfortably exceed
#: the gap between a client noticing expiry and acting on it (a backgrounded
#: mobile app, a provider stepping out mid-visit), and stay well under the 4h
#: ``procedure_seen:{session_id}`` TTL that already bounds a visit's other
#: server-side state. Overridable through ``SESSION_REMINT_GRACE_SECONDS``, and
#: ``tests/unit/test_remint_credential.py`` proves the behaviour follows the
#: setting rather than this literal, so changing it is a config edit.
DEFAULT_REMINT_GRACE_SECONDS: Final = 3600

#: HS256 keys shorter than the hash output weaken the MAC, and PyJWT warns about
#: them. Enforced here so a placeholder secret cannot reach a running service.
MIN_SIGNING_KEY_BYTES: Final = 32

#: Symmetric signing is deliberate for v1 — every service that validates these
#: tokens is first-party and already shares the secret.
JWT_ALGORITHM: Final = "HS256"

#: Every AWS service this platform uses is HIPAA-eligible in us-east-1 and the
#: BAA is signed for that region. Matches ``track_b_rag.config``.
DEFAULT_AWS_REGION: Final = "us-east-1"

#: Sonnet, for the SOAP note itself: CLAUDE.md's Bedrock Model Assignment table
#: names "TASK-030 SOAP note generation" as long-form structured clinical
#: writing. The default exists so local dev works unset; code reads the setting
#: and never a literal model id.
DEFAULT_BEDROCK_MODEL_ID_REASONING: Final = "anthropic.claude-sonnet-4-6"

#: Haiku, for the ICD-10/CPT pass in the same task — extraction rather than
#: reasoning, and 15x cheaper per call. The two ids are separate settings
#: because TASK-030 makes both calls, deliberately, rather than asking one
#: model to write the note and pull the codes out of it.
DEFAULT_BEDROCK_MODEL_ID_FAST: Final = "anthropic.claude-haiku-4-5-20251001"


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
    session_remint_grace_seconds: int = Field(default=DEFAULT_REMINT_GRACE_SECONDS, gt=0)
    redis_url: str = Field(default="redis://localhost:6379/0", min_length=1)
    #: Browser origins this service answers, from ``CORS_ALLOWED_ORIGINS``.
    #: Empty by default, so an unconfigured deployment answers no browser rather
    #: than trusting one nobody chose — a localhost origin baked in as a default
    #: would ship to production the moment the variable was forgotten. Local dev
    #: gets its value from ``.env.example``. See CLAUDE.md, "CORS and browser
    #: reachability".
    cors_allowed_origins: AllowedOrigins = ()

    aws_region: str = Field(default=DEFAULT_AWS_REGION, min_length=1)
    #: Sonnet — the SOAP note. See :mod:`track_a_clinical.soap`.
    bedrock_model_id_reasoning: str = Field(
        default=DEFAULT_BEDROCK_MODEL_ID_REASONING,
        min_length=1,
    )
    #: Haiku — the ICD-10/CPT extraction pass.
    bedrock_model_id_fast: str = Field(
        default=DEFAULT_BEDROCK_MODEL_ID_FAST,
        min_length=1,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment once.

    Tests that change the environment must call ``get_settings.cache_clear()``.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment

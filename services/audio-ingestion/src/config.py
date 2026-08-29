"""Runtime configuration for the audio-ingestion service.

Values come from the process environment only, for the reasons given in
``track_a_clinical.config``: local development exports them from ``.env.local``,
CI sets them on the job, deployments inject them from AWS Secrets Manager, and
reading a file here would add a fourth source of truth and a tempting place to
commit a secret.

``jwt_signing_key`` carries the same 32-byte floor track-a-clinical enforces on
it, taken from ``session_auth`` rather than restated here (TASK-041). The two
services must agree about the key or every connection is rejected, so they must
also agree about what counts as an acceptable one — a validator that accepted a
key the issuer refuses would turn a configuration mistake into a mystery at
connection time.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from cors_policy import AllowedOrigins
from session_auth import MIN_SIGNING_KEY_BYTES

#: 16kHz mono is what TASK-022 and TASK-023 capture and what Transcribe Medical
#: wants for high-quality audio. A sample rate that disagrees with the audio
#: makes the stream hang rather than error, so this is configuration a deployment
#: can correct without a release.
DEFAULT_SAMPLE_RATE_HZ: Final = 16_000

#: Signed 16-bit little-endian PCM, the only encoding both capture clients
#: produce without a transcode step.
DEFAULT_MEDIA_ENCODING: Final = "pcm"


class Settings(BaseSettings):
    """Environment-backed settings for the audio WebSocket and its transcription."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    jwt_signing_key: str = Field(min_length=MIN_SIGNING_KEY_BYTES)
    redis_url: str = Field(default="redis://localhost:6379/0", min_length=1)
    aws_region: str = Field(default="us-east-1", min_length=1)
    #: Browser origins this service answers, from ``CORS_ALLOWED_ORIGINS``.
    #: Empty by default, so an unconfigured deployment answers no browser rather
    #: than trusting one nobody chose — a localhost origin baked in as a default
    #: would ship to production the moment the variable was forgotten. Local dev
    #: gets its value from ``.env.example``. See CLAUDE.md, "CORS and browser
    #: reachability".
    cors_allowed_origins: AllowedOrigins = ()

    transcribe_medical_language_code: str = Field(default="en-US", min_length=2)
    transcribe_medical_specialty: str = Field(default="PRIMARYCARE", min_length=1)
    transcribe_medical_type: str = Field(default="CONVERSATION", min_length=1)
    transcribe_medical_sample_rate_hz: int = Field(default=DEFAULT_SAMPLE_RATE_HZ, gt=0)
    transcribe_medical_media_encoding: str = Field(default=DEFAULT_MEDIA_ENCODING, min_length=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment once.

    Tests that change the environment must call ``get_settings.cache_clear()``.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment

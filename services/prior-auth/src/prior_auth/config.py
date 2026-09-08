"""Runtime configuration for the prior-auth service.

Values come from the process environment only — no ``.env`` file is read here,
for the reason track-a-clinical's config module gives: local development
exports them from ``.env.local``, CI sets them on the job, and deployments
inject them from AWS Secrets Manager, and reading a file from inside the
service would add a fourth source of truth and a tempting place to commit a
secret.

**Every setting here is read by something.** An entry that exists in
``.env.example`` and is bound by nothing is a setting that looks configured and
is not — the trap CLAUDE.md names, and one this repository has already found
three times.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: ``fhir-integration``'s base URL, from the local dev port table in CLAUDE.md.
#: The assembler reads the patient's demographics, coverage and conditions
#: through it. The default exists so local dev works unset; code reads the
#: setting and never a literal.
DEFAULT_FHIR_INTEGRATION_URL: Final = "http://localhost:8004"

#: How long to wait for that read. **A round-number default, not a
#: measurement.** Nothing is watching a button for it — assembly happens after
#: the visit has ended — so it is deliberately more generous than
#: track-a-clinical's 8s budget for the same service at session start, where a
#: provider is waiting. The call behind it is three FHIR round trips to an EHR.
DEFAULT_FHIR_INTEGRATION_TIMEOUT_SECONDS: Final = 15.0

#: The backoff between attempts to load a note that TASK-030 has not finished
#: writing, in seconds. TASK-060 specifies 3 attempts at 2s/4s/8s: the SOAP pass
#: is a Sonnet call followed by a Haiku call and a Comprehend Medical call, and
#: the end signal reaches both services at once, so the note routinely does not
#: exist at the first look.
#:
#: **These are the task's numbers, not measured ones.** What brackets them is
#: that the total (14s) has to exceed a normal generation and stay far short of
#: the 4h ``procedure_seen:`` TTL that bounds a visit's other state. Overridable
#: through ``NOTE_RETRY_DELAYS`` so a deployment that observes slower
#: generations can widen it without a release.
DEFAULT_NOTE_RETRY_DELAYS: Final = (2.0, 4.0, 8.0)


class Settings(BaseSettings):
    """Environment-backed settings for bundle assembly."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    redis_url: str = Field(default="redis://localhost:6379/0", min_length=1)

    #: Where the patient context is read from. Over HTTP rather than by import,
    #: so the ``READ_PATIENT`` audit row that service's route writes cannot be
    #: bypassed — the same arrangement, and the same argument, as
    #: track-a-clinical's coverage-context read at session start.
    fhir_integration_url: str = Field(
        default=DEFAULT_FHIR_INTEGRATION_URL,
        min_length=1,
    )
    fhir_integration_timeout_seconds: float = Field(
        default=DEFAULT_FHIR_INTEGRATION_TIMEOUT_SECONDS,
        gt=0,
    )

    #: Waits between note-load attempts. An empty tuple means "look once and
    #: give up", which is a legitimate configuration rather than a broken one:
    #: the number of attempts is one more than the number of delays.
    note_retry_delays: tuple[float, ...] = DEFAULT_NOTE_RETRY_DELAYS


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment once.

    Tests that change the environment must call ``get_settings.cache_clear()``.
    """
    return Settings()

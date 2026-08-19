"""Runtime configuration for the track-b-rag service.

Values come from the process environment only, matching track-a-clinical: local
development exports them from ``.env.local``, CI sets them on the job, and
deployments inject them from AWS Secrets Manager. Reading a ``.env`` file from
inside the service would add a fourth source of truth and a tempting place to
commit a secret.

The names here are the ones already in ``.env.example`` — ``QDRANT_HOST``,
``QDRANT_PORT``, ``QDRANT_API_KEY``, ``QDRANT_COLLECTION``,
``EMBEDDING_MODEL_NAME``, ``EMBEDDING_DIMENSIONS``. Every one has a working
local-dev default, so the service starts against ``docker compose up`` with no
environment set at all.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Final

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The Qdrant collection holding insurance policy chunks. Matches
#: ``DEFAULT_QDRANT_COLLECTION`` on the shared ``InsurancePolicy`` model, which
#: records the collection each ingested document was indexed into.
DEFAULT_QDRANT_COLLECTION: Final = "insurance_policies"

#: Local sentence-transformers model, per CLAUDE.md's Tech Stack. Local means no
#: external API call and therefore no policy text leaving the cluster.
DEFAULT_EMBEDDING_MODEL_NAME: Final = "BAAI/bge-large-en-v1.5"

#: bge-large's output width. Fixed by the model, not a tunable — the Qdrant
#: collection is created with this size and a mismatch is rejected on upsert.
DEFAULT_EMBEDDING_DIMENSIONS: Final = 1024


def _empty_to_none(value: object) -> object:
    """Treat an empty environment variable as unset.

    ``.env.example`` ships every key with no value, so a shell that sources it
    exports ``QDRANT_API_KEY=`` — which is an unauthenticated local Qdrant, not
    an API key that happens to be the empty string.
    """
    return None if value == "" else value


OptionalSecret = Annotated[str | None, BeforeValidator(_empty_to_none)]


class Settings(BaseSettings):
    """Environment-backed settings for the vector store and the embedder."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    qdrant_host: str = Field(default="localhost", min_length=1)
    qdrant_port: int = Field(default=6333, gt=0, lt=65536)
    #: Unset for local dev and CI; a real key in every deployed environment.
    qdrant_api_key: OptionalSecret = None
    qdrant_collection: str = Field(default=DEFAULT_QDRANT_COLLECTION, min_length=1)

    embedding_model_name: str = Field(default=DEFAULT_EMBEDDING_MODEL_NAME, min_length=1)
    embedding_dimensions: int = Field(default=DEFAULT_EMBEDDING_DIMENSIONS, gt=0)

    @property
    def qdrant_url(self) -> str:
        """Return the Qdrant REST base URL.

        Plain HTTP is correct only for the docker-compose container on
        localhost; deployed environments terminate TLS in front of Qdrant and
        set ``QDRANT_HOST`` to that endpoint.
        """
        return f"http://{self.qdrant_host}:{self.qdrant_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment once.

    Tests that change the environment must call ``get_settings.cache_clear()``.
    """
    return Settings()

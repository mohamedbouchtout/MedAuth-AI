"""Runtime configuration for the track-b-rag service.

Values come from the process environment only, matching track-a-clinical: local
development exports them from ``.env.local``, CI sets them on the job, and
deployments inject them from AWS Secrets Manager. Reading a ``.env`` file from
inside the service would add a fourth source of truth and a tempting place to
commit a secret.

The names here are the ones already in ``.env.example`` — ``QDRANT_HOST``,
``QDRANT_PORT``, ``QDRANT_API_KEY``, ``QDRANT_COLLECTION``,
``EMBEDDING_MODEL_NAME``, ``EMBEDDING_DIMENSIONS``, and, added for the query
endpoint in TASK-012, ``REDIS_URL``, ``AWS_REGION`` and
``BEDROCK_MODEL_ID_REASONING``. Every one has a working local-dev default, so
the service starts against ``docker compose up`` with no environment set at all.

TASK-012 deliberately introduces no *new* environment variable: the three names
it starts reading were already in ``.env.example``, sitting unused. The cache
TTL and the retrieval depth are constants in the modules that own them rather
than settings, because CLAUDE.md fixes both values and a knob nobody is meant
to turn is a knob that eventually disagrees with the document.
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

#: Local Redis from ``docker-compose.yml``. Database 0, matching every other
#: service — the cache and the pub/sub channels share one instance.
DEFAULT_REDIS_URL: Final = "redis://localhost:6379/0"

#: us-east-1 only, per CLAUDE.md: that is where the HIPAA-eligible services
#: covered by the signed BAA live.
DEFAULT_AWS_REGION: Final = "us-east-1"

#: Sonnet, because ``/policies/query`` reasons over retrieved policy text rather
#: than extracting from it — CLAUDE.md's Bedrock Model Assignment table names
#: this call site explicitly. The default exists so local dev works unset; code
#: reads the setting and never a literal model id.
DEFAULT_BEDROCK_MODEL_ID_REASONING: Final = "anthropic.claude-sonnet-4-6"


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

    #: Cache for the payer-policy half of a query answer, and nothing else — see
    #: :mod:`track_b_rag.cache` for what may and may not be written under it.
    redis_url: str = Field(default=DEFAULT_REDIS_URL, min_length=1)

    aws_region: str = Field(default=DEFAULT_AWS_REGION, min_length=1)
    bedrock_model_id_reasoning: str = Field(
        default=DEFAULT_BEDROCK_MODEL_ID_REASONING,
        min_length=1,
    )

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

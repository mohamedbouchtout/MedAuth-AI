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
``BEDROCK_MODEL_ID_REASONING``. TASK-015 adds ``CRD_BASE_URL`` and
``CRD_TIMEOUT_SECONDS``, and TASK-021 adds ``POLICY_QUERY_BASE_URL`` and
``POLICY_QUERY_TIMEOUT_SECONDS``, all four new to ``.env.example`` rather than
names already sitting there. Every one has a working local-dev default, so the
service starts against ``docker compose up`` with no environment set at all.

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

from cors_policy import AllowedOrigins

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

#: The CRD Reference Implementation from ``docker-compose.yml``. In a deployed
#: environment this is the payer's own endpoint. Empty turns the tier off
#: entirely and every query takes the RAG path, exactly as before TASK-015.
DEFAULT_CRD_BASE_URL: Final = "http://localhost:8006"

#: Measured against the Reference Implementation on a developer machine: ~0.5s
#: steady state, ~3.0s on the first request while it compiles its CQL rule
#: libraries. Four seconds clears the cold start and still bounds a call that
#: sits in the nudge path. It is not a knob for making a slow payer work — a
#: payer that cannot answer in four seconds should fall through to RAG, which
#: is what a timeout does.
DEFAULT_CRD_TIMEOUT_SECONDS: Final = 4.0

#: This service's own base URL. The transcript consumer calls
#: ``POST /policies/query`` over HTTP rather than importing the function behind
#: it, so that the route layer's ``audit_log()`` write stays the single place a
#: PHI-carrying policy query is recorded — see
#: :mod:`track_b_rag.policy_dispatch`. Configurable because the loopback address
#: differs between a laptop, a pod and a test.
DEFAULT_POLICY_QUERY_BASE_URL: Final = "http://localhost:8002"

#: Generous next to the CRD tier's four seconds, because this call is the whole
#: query: a cache miss embeds the question, searches Qdrant and reasons over the
#: retrieved policy text with Sonnet. It bounds a hung connection rather than
#: expressing a latency target — a query that takes this long has already missed
#: the moment it was meant to inform.
DEFAULT_POLICY_QUERY_TIMEOUT_SECONDS: Final = 15.0

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

    #: The Da Vinci CRD tier (TASK-015). ``None`` disables it; the RAG path is
    #: unaffected either way, which is what makes the tier safe to turn off.
    crd_base_url: OptionalSecret = DEFAULT_CRD_BASE_URL
    crd_timeout_seconds: float = Field(default=DEFAULT_CRD_TIMEOUT_SECONDS, gt=0)

    #: Where the transcript consumer (TASK-021) sends its policy queries. This
    #: service's own address: the caller and the route are the same process.
    policy_query_base_url: str = Field(default=DEFAULT_POLICY_QUERY_BASE_URL, min_length=1)
    policy_query_timeout_seconds: float = Field(
        default=DEFAULT_POLICY_QUERY_TIMEOUT_SECONDS,
        gt=0,
    )

    aws_region: str = Field(default=DEFAULT_AWS_REGION, min_length=1)
    bedrock_model_id_reasoning: str = Field(
        default=DEFAULT_BEDROCK_MODEL_ID_REASONING,
        min_length=1,
    )

    #: Browser origins this service answers, from ``CORS_ALLOWED_ORIGINS``.
    #: Empty by default, so an unconfigured deployment answers no browser rather
    #: than trusting one nobody chose — a localhost origin baked in as a default
    #: would ship to production the moment the variable was forgotten. Local dev
    #: gets its value from ``.env.example``. See CLAUDE.md, "CORS and browser
    #: reachability".
    cors_allowed_origins: AllowedOrigins = ()

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

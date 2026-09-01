"""Request-scoped dependencies: Redis, an HTTP client, and settings.

All three are declared as FastAPI dependencies rather than reached for directly,
so a test can substitute a fake through ``app.dependency_overrides``. That is
what lets the launch suite drive a whole OAuth round trip with neither a Redis
server nor an authorization server in reach.

``require_credentials()`` sits here too, though it is a helper rather than a
dependency: both routers need it — the launch flow to obtain a token and
TASK-051b's renewal to refresh one — and it reads settings to answer.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Final

import httpx
from fastapi import status
from redis.asyncio import Redis

from api_envelope import ApiHTTPException
from src.adapters.factory import EHRType
from src.config import (
    ClientCredentials,
    MissingClientCredentialsError,
    Settings,
    get_settings,
)

logger = logging.getLogger(__name__)

ERROR_CODE_CLIENT_NOT_REGISTERED: Final = "SMART_CLIENT_NOT_REGISTERED"


@lru_cache(maxsize=1)
def _redis_client() -> Redis:
    """Return the process-wide Redis client, connected lazily on first command."""
    return Redis.from_url(get_settings().redis_url)


@lru_cache(maxsize=1)
def _http_client() -> httpx.AsyncClient:
    """Return the process-wide HTTP client used to talk to EHRs.

    One client rather than one per request, so connections to a vendor's
    authorization server are pooled across launches. Timeouts are set per call
    rather than here: discovery and the token exchange each state their own.
    """
    return httpx.AsyncClient(follow_redirects=True)


async def get_redis() -> Redis:
    """Return the Redis client the launch records are held in."""
    return _redis_client()


async def get_http_client() -> httpx.AsyncClient:
    """Return the HTTP client used for discovery and token exchange."""
    return _http_client()


async def get_app_settings() -> Settings:
    """Return the process-wide settings."""
    return get_settings()


async def close_clients() -> None:
    """Close both clients and forget them. Called on app shutdown."""
    if _redis_client.cache_info().currsize:
        await _redis_client().aclose()
    _redis_client.cache_clear()

    if _http_client.cache_info().currsize:
        await _http_client().aclose()
    _http_client.cache_clear()


def require_credentials(settings: Settings, ehr_type: EHRType) -> ClientCredentials:
    """Return the registered client for an EHR, or fail with a named error.

    Shared by the launch flow and by token renewal because both authenticate the
    same client to the same authorization server. A second copy would be a
    second answer to "which registration is this vendor's", which is what
    ``EHRType`` exists to stop there being.

    Raises:
        ApiHTTPException: 500 when no ``client_id`` is configured for the
            vendor. The message names the environment variable to set and never
            a secret's value.
    """
    try:
        return settings.credentials_for(ehr_type)
    except MissingClientCredentialsError as exc:
        # exc names the environment variable to set and never a secret's value.
        logger.error("No SMART client registered: %s", exc)
        raise ApiHTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ERROR_CODE_CLIENT_NOT_REGISTERED,
            f"No SMART client registered for EHR '{ehr_type.value}'",
        ) from None

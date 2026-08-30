"""Request-scoped dependencies: Redis, an HTTP client, and settings.

All three are declared as FastAPI dependencies rather than reached for directly,
so a test can substitute a fake through ``app.dependency_overrides``. That is
what lets the launch suite drive a whole OAuth round trip with neither a Redis
server nor an authorization server in reach.
"""

from __future__ import annotations

from functools import lru_cache

import httpx
from redis.asyncio import Redis

from src.config import Settings, get_settings


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

"""Request-scoped dependencies: Redis, settings, and the transcription factory.

All three are declared as FastAPI dependencies rather than reached for directly,
so a test can substitute a fake through ``app.dependency_overrides``. That is
what lets the WebSocket suite drive a whole connection with no AWS account and
no Redis in reach — FastAPI resolves dependencies for WebSocket routes exactly
as it does for HTTP ones.
"""

from __future__ import annotations

from functools import lru_cache

from redis.asyncio import Redis

from src.config import Settings, get_settings
from src.transcribe_medical import open_medical_stream
from src.transcription import TranscriptionStream, TranscriptionStreamFactory


@lru_cache(maxsize=1)
def _redis_client() -> Redis:
    """Return the process-wide Redis client, connected lazily on first command."""
    return Redis.from_url(get_settings().redis_url)


async def get_redis() -> Redis:
    """Return the Redis client transcript segments are published on."""
    return _redis_client()


async def close_redis() -> None:
    """Close the Redis client and forget it. Called on app shutdown."""
    if _redis_client.cache_info().currsize:
        await _redis_client().aclose()
    _redis_client.cache_clear()


async def get_app_settings() -> Settings:
    """Return the process-wide settings."""
    return get_settings()


async def get_transcription_factory() -> TranscriptionStreamFactory:
    """Return the factory that opens one Transcribe Medical stream per connection.

    The factory is what tests replace. Overriding it, rather than patching AWS
    internals, is what keeps the route's own logic — buffering, publishing,
    teardown — under test without a network.
    """

    async def factory() -> TranscriptionStream:
        return await open_medical_stream(get_settings())

    return factory

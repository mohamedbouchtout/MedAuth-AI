"""The Redis cache for the payer-policy half of a query answer.

**What may be written here is the whole point of this module.** A
``/policies/query`` response mixes two kinds of data, and only one of them is
shareable between patients:

* *payer-policy fields* — ``requires_auth``, ``auth_criteria``,
  ``step_therapy_required``, ``step_therapy_details``. These describe the
  payer's rules for a procedure and are identical for every patient with the
  same payer, plan type, state and CPT code. They are what the key
  ``rag:{payer}:{plan_type}:{state}:{cpt_code}`` addresses.
* *patient-specific fields* — ``missing_criteria``, ``denial_risk``,
  ``nudge_message``. These describe *this encounter's documentation* and are
  recomputed on every call.

Caching the second group under a key that does not mention the patient would
serve one patient the documentation gaps computed for another. That is a
patient-safety defect rather than a stale-cache annoyance, which is why the
split is enforced by this module's signatures: nothing here accepts anything
but a :class:`~track_b_rag.policy_rules.PolicyRules` payload, serialised by its
caller. CLAUDE.md's cache note and TASK-012 both record the reasoning.

The client is a lazily-created singleton, like Qdrant's. A cache is not a
correctness dependency: every read and write here degrades to "no cache" when
Redis is unreachable, and the request continues at full cost. ``GET /health``
is where an unreachable Redis becomes visible, because paying Bedrock for every
query is an outage worth naming even though no request fails.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from track_b_rag.config import get_settings

logger = logging.getLogger(__name__)

#: 24 hours, per CLAUDE.md's Redis key list. Payer policies change on the order
#: of months and the nightly scraper (TASK-013) re-ingests them, so a day-old
#: answer is current; the TTL exists to bound how long a withdrawn policy can
#: keep answering, not to track a fast-moving source.
POLICY_RULES_TTL_SECONDS: Final = 24 * 60 * 60


def policy_rules_key(*, payer: str, plan_type: str, state: str, cpt_code: str) -> str:
    """Return the cache key for one payer/plan/state/procedure combination.

    Exactly the pattern CLAUDE.md's canonical Redis key list fixes:
    ``rag:{payer}:{plan_type}:{state}:{cpt_code}``. The segments are the request
    values as validated, not a normalised copy of them — the same values also
    build the Qdrant filter on a miss, so a key and the retrieval it stands for
    cannot disagree about which payer they mean.
    """
    return f"rag:{payer}:{plan_type}:{state}:{cpt_code}"


@lru_cache(maxsize=1)
def get_client() -> Redis:
    """Return the process-wide Redis client, connected lazily on first command."""
    return Redis.from_url(get_settings().redis_url)


async def close_client() -> None:
    """Close the Redis client and forget it. Called on app shutdown."""
    if get_client.cache_info().currsize:
        await get_client().aclose()
    get_client.cache_clear()


async def check_health(client: Redis) -> bool:
    """Return whether Redis answers. Used by ``GET /health``.

    Any failure is a failure: the endpoint reports one flag per dependency and
    the exception detail goes to the log rather than into the response body.
    """
    try:
        await client.ping()
    except Exception:  # any transport or server fault is "not ok"
        logger.warning("Redis health check failed", exc_info=True)
        return False
    return True


async def get_cached(client: Redis, key: str) -> str | None:
    """Return the cached JSON document at `key`, or None for a miss.

    An unreachable Redis is reported as a miss rather than raised: the caller's
    fallback for a miss is to do the work, which is exactly the right behaviour
    when the cache is down. The log line carries the key, which holds payer and
    procedure identifiers and no patient data.
    """
    try:
        raw = await client.get(key)
    except RedisError:
        logger.warning("Redis read failed for %r; treating as a cache miss", key, exc_info=True)
        return None

    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


async def set_cached(client: Redis, key: str, value: str, ttl_seconds: int) -> bool:
    """Write `value` at `key` with a TTL, reporting whether the write landed.

    Returns False when Redis refused or was unreachable. A failed cache write is
    not a failed request — the answer the caller already computed is still
    correct, it just will not be reused.
    """
    try:
        await client.set(key, value, ex=ttl_seconds)
    except RedisError:
        logger.warning("Redis write failed for %r; the answer was not cached", key, exc_info=True)
        return False
    return True

"""Moving nudges from the Redis bus to one connected client, unaltered.

``track-b-rag`` publishes a nudge to ``nudges:{session_id}`` (TASK-040) and knows
nothing about who reads it. This service subscribes on behalf of one client and
forwards what arrives.

**The payload is relayed as the raw string it arrived as, and is never parsed.**
CLAUDE.md's "The nudge payload — one shape" is the contract, with one writer and
four readers, and a relay that deserialized and re-serialized it would be a
second definition of that shape — free to drift from the one that writes it, and
positioned where nothing would notice. Modelling it here would also mean this
service had to be changed for TASK-044's keyword-only nudge, which differs only
in a field this module has no reason to know exists. So the relay knows the
channel name and nothing about the message.

That is a deliberate trade: a malformed publish reaches the client as-is rather
than being caught here. It is the right one. This service cannot repair a bad
payload, and dropping messages it fails to parse would turn a formatting bug in
the emitter into silence at the bedside — the failure mode CLAUDE.md rejects
everywhere else, where a provider cannot tell "nothing to flag" from "something
broke".

**Subscription is per session, by name.** Pattern-subscribing ``nudges:*`` would
put a wildcard across the channel family carrying clinical alerts and hand one
client every encounter in the clinic. The session id comes from the validated
token, so a client can only ever subscribe to the encounter its token names.

**PHI discipline.** Nudge text reaches this module and leaves it over the socket.
It is never logged: log lines here carry session identifiers and message counts,
never message content.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Final

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

#: CLAUDE.md's canonical Redis key list, formatted here rather than at call sites
#: so a variant spelling cannot appear in one place and go unnoticed. Each service
#: formats these patterns locally — the same arrangement audio-ingestion uses for
#: the publishing side.
CHANNEL_TEMPLATE: Final = "nudges:{session_id}"

#: How long a read waits before looping. Not a latency budget — a message arriving
#: mid-wait wakes the read immediately. It only bounds how quickly a cancelled
#: task notices it was cancelled.
READ_TIMEOUT_SECONDS: Final = 1.0


def channel_for(session_id: uuid.UUID) -> str:
    """Return the nudge channel for one session."""
    return CHANNEL_TEMPLATE.format(session_id=session_id)


def decode_payload(data: object) -> str | None:
    """Return a Redis payload as the text to relay, or None if it cannot be.

    A WebSocket text frame carries UTF-8, so a payload that is not valid UTF-8
    cannot be relayed at all. That should never happen — the emitter publishes
    ``json.dumps`` output — and if it does, dropping the one message is better
    than letting the decode error tear down a live encounter's connection. The
    log line names neither the payload nor its content.
    """
    if isinstance(data, str):
        return data
    if isinstance(data, bytes):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Dropped a nudge payload that was not valid UTF-8")
            return None
    logger.warning("Dropped a nudge payload of unexpected type %s", type(data).__name__)
    return None


def is_nudge_message(message: Any) -> bool:
    """Return whether a redis-py message is a published nudge.

    Subscribe and unsubscribe confirmations arrive on the same connection and are
    not messages to relay. ``ignore_subscribe_messages`` already filters them;
    this is the belt to that braces, and it keeps the caller honest if that flag
    is ever changed.
    """
    return bool(message) and message.get("type") == "message"


async def check_health(redis: Redis) -> bool:
    """Return whether Redis answers. Used by ``GET /health``."""
    try:
        await redis.ping()
    except Exception:
        logger.warning("Redis health check failed", exc_info=True)
        return False
    return True

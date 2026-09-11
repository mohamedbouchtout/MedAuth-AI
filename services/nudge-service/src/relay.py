"""Moving messages from the Redis bus to one connected client, unaltered.

This service relays two channel families and knows the contents of neither.
``track-b-rag`` publishes a nudge to ``nudges:{session_id}`` (TASK-040) and
``audio-ingestion`` publishes a transcript segment to
``transcription:{session_id}`` (TASK-020); each knows nothing about who reads it.
This service subscribes on behalf of one client and forwards what arrives.

**Nothing here is nudge-specific, and it used to be.** TASK-041d added the
transcript relay, and the route lifecycle around these helpers was already
channel-agnostic apart from three log strings and a channel name — so the
alternative was a second copy of a working relay. That is the arrangement
TASK-041 itself refused when it extracted ``packages/session-auth`` rather than
let ``audio-ingestion``'s validator be copied, on the grounds that copies diverge
not on purpose but because a fix lands in whichever file the person had open.
These helpers therefore take the channel as an argument, and the log lines name
it, so a reader of an operational trace can tell which stream dropped a message.

**The payload is relayed as the raw string it arrived as, and is never parsed.**
Each payload's shape is fixed in CLAUDE.md — "The nudge payload — one shape" and
"The transcript segment payload — one shape" — each with one writer and several
readers. A relay that deserialized and re-serialized either would be a second
definition of that shape, free to drift from the one that writes it and
positioned where nothing would notice. Modelling the nudge here would also mean
this service had to change for TASK-044's keyword-only nudge, which differs only
in a field this module has no reason to know exists. So the relay knows the
channel name and nothing about the message.

That is a deliberate trade: a malformed publish reaches the client as-is rather
than being caught here. It is the right one. This service cannot repair a bad
payload, and dropping messages it fails to parse would turn a formatting bug in a
publisher into silence at the bedside — the failure mode CLAUDE.md rejects
everywhere else, where a provider cannot tell "nothing to flag" from "something
broke".

**Subscription is per session, by name.** Pattern-subscribing ``nudges:*`` would
put a wildcard across the channel family carrying clinical alerts and hand one
client every encounter in the clinic; ``transcription:*`` would do the same for
the family carrying speech, which is worse. The session id comes from the
validated token, so a client can only ever subscribe to the encounter its token
names.

**PHI discipline.** Nudge text and transcript text both reach this module and
leave it over a socket. Neither is ever logged: log lines here carry a channel
name, a session identifier or a message count, never message content. A
transcript segment is the largest body of PHI on any bus in this repository.
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
NUDGE_CHANNEL_TEMPLATE: Final = "nudges:{session_id}"
TRANSCRIPT_CHANNEL_TEMPLATE: Final = "transcription:{session_id}"

#: How long a read waits before looping. Not a latency budget — a message arriving
#: mid-wait wakes the read immediately. It only bounds how quickly a cancelled
#: task notices it was cancelled.
READ_TIMEOUT_SECONDS: Final = 1.0


def channel_for(template: str, session_id: uuid.UUID) -> str:
    """Return one session's channel for the given canonical key template."""
    return template.format(session_id=session_id)


def decode_payload(data: object, *, channel: str) -> str | None:
    """Return a Redis payload as the text to relay, or None if it cannot be.

    A WebSocket text frame carries UTF-8, so a payload that is not valid UTF-8
    cannot be relayed at all. That should never happen — every publisher on both
    channels emits ``json.dumps`` output — and if it does, dropping the one
    message is better than letting the decode error tear down a live encounter's
    connection.

    The log line names the channel the message came from and never the payload or
    its content. The channel is what makes the line useful once this service
    relays more than one stream; it carries a session identifier, which is logged
    freely here, and never any part of the message.
    """
    if isinstance(data, str):
        return data
    if isinstance(data, bytes):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Dropped a payload on %s that was not valid UTF-8", channel)
            return None
    logger.warning("Dropped a payload on %s of unexpected type %s", channel, type(data).__name__)
    return None


def is_published_message(message: Any) -> bool:
    """Return whether a redis-py message is a published payload.

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

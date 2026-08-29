"""The audit row this service writes, and the reasoning for writing exactly one.

Known Constraints #6 in TASKS.md: ``audit_log()`` is called if and only if a
route touches PHI. The audio WebSocket plainly does — it carries a recording of
a clinician and a patient talking, and the transcript it produces is clinical
content about an identified encounter — so a connection that is accepted writes
an audit row.

Three decisions about *which* accesses become rows:

* **One row per connection, not per segment.** A ten-minute encounter produces
  hundreds of segments from a single act of access by a single provider. A row
  each would bury the compliance signal in volume without adding a fact anyone
  would query for.
* **A rejected connection writes no row.** No PHI was reached: the token was
  refused before the handshake completed and before any transcription stream
  existed. Failed authentication is an operational event and is logged as one,
  at WARNING, through ``logging.getLogger``.
* **The row is written on accept, before audio flows,** so an encounter cannot be
  streamed without the access being recorded. Unlike track-a-clinical there is no
  request transaction here to join — this service owns no tables and holds no
  database session — so the write goes on hipaa-logger's own pool.

``resource_id`` is the session identifier, not an ``encounters`` primary key.
This service is not permitted to know the latter: it validates a JWT and never
reads the encounters table. The session id is what correlates this row with
track-a-clinical's ``START_SESSION`` row for the same encounter.
"""

from __future__ import annotations

import uuid
from typing import Final

from hipaa_logger import AuditAction, audit_log

#: The access this service records: a provider streamed encounter audio through
#: transcription. The action itself is ``AuditAction.STREAM_AUDIO`` — the
#: vocabulary is defined once in hipaa-logger and imported, never re-declared as
#: a local string.
SERVICE_NAME: Final = "audio-ingestion"
RESOURCE_TYPE_ENCOUNTER: Final = "Encounter"


async def audit_audio_stream(
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Record that encounter audio was streamed under one session.

    Args:
        session_id: The encounter's session identifier, from the validated token.
        provider_id: The provider the token was minted for.
        ip_address: The connecting client's address, when the server reported one.
        user_agent: The connecting client's user agent, when it sent one.
    """
    await audit_log(
        actor_id=str(provider_id),
        action=AuditAction.STREAM_AUDIO,
        resource_type=RESOURCE_TYPE_ENCOUNTER,
        resource_id=str(session_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        ip_address=ip_address,
        user_agent=user_agent,
    )

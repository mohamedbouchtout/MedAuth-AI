"""The audit row this service writes, and the reasoning for writing exactly one.

Known Constraints #6 in TASKS.md: ``audit_log()`` is called if and only if a
route touches PHI. The nudge socket does. What crosses it names a procedure, the
payer criteria still undocumented for it, and a denial risk, all scoped to one
identified encounter — see CLAUDE.md, "The nudge payload — one shape". This
service holds no database session and reads no tables, so the row written here is
the only record anywhere that a client watched an encounter's nudges.

The three decisions about *which* accesses become rows are the same ones
``audio-ingestion`` made, for the same reasons:

* **One row per connection, not per relayed nudge.** A visit is one act of access
  by one provider. A row per message would bury the compliance signal under
  volume without adding a fact anyone would query for, and CLAUDE.md's
  consumer-auditing section states the rule directly: one row per unit of work.
* **A refused connection writes no row.** No PHI was reached — the token was
  refused before the handshake completed and before any subscription existed.
  Failed authentication is an operational event and is logged as one, at WARNING.
* **The row is written on accept, before any nudge is relayed,** so a stream
  cannot be watched without the access being recorded. There is no request
  transaction to join, so the write goes on hipaa-logger's own pool.

``resource_type`` is ``Encounter`` rather than ``ClinicalNudge``: the access is to
one encounter's stream, not to a particular nudge row, and this service never
reads the ``clinical_nudges`` table — ``track-b-rag`` writes it and audits that
separately as ``WRITE_NUDGE``. ``resource_id`` is the session identifier, which is
what correlates this row with track-a-clinical's ``START_SESSION`` row and with
audio-ingestion's ``STREAM_AUDIO`` row for the same visit.
"""

from __future__ import annotations

import uuid
from typing import Final

from hipaa_logger import audit_log

#: The access this service records: a client opened one encounter's nudge stream.
#: From CLAUDE.md's action vocabulary, which carries this action for this service.
ACTION_RELAY_NUDGES: Final = "RELAY_NUDGES"

SERVICE_NAME: Final = "nudge-service"
RESOURCE_TYPE_ENCOUNTER: Final = "Encounter"


async def audit_nudge_stream(
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Record that one encounter's nudge stream was opened to a client.

    Args:
        session_id: The encounter's session identifier, from the validated token.
        provider_id: The provider the token was minted for. Taken from the token
            rather than from a database: this service reads no tables, and the
            token's claim is what track-a-clinical recorded on the encounter.
        ip_address: The connecting client's address, when the server reported one.
        user_agent: The connecting client's user agent, when it sent one.
    """
    await audit_log(
        actor_id=str(provider_id),
        action=ACTION_RELAY_NUDGES,
        resource_type=RESOURCE_TYPE_ENCOUNTER,
        resource_id=str(session_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        ip_address=ip_address,
        user_agent=user_agent,
    )

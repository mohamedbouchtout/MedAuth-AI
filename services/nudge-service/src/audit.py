"""The audit rows this service writes, and the reasoning for writing exactly one.

Known Constraints #6 in TASKS.md: ``audit_log()`` is called if and only if a
route touches PHI. Both of this service's sockets do. What crosses the nudge
socket names a procedure, the payer criteria still undocumented for it, and a
denial risk, all scoped to one identified encounter — see CLAUDE.md, "The nudge
payload — one shape". What crosses the transcript socket is what was *said*
during that encounter, which is the largest body of PHI on any bus here. This
service holds no database session and reads no tables, so the row written here is
the only record anywhere that a client watched either stream.

The three decisions about *which* accesses become rows are the same ones
``audio-ingestion`` made, for the same reasons, and they apply identically to
both streams:

* **One row per connection, not per relayed message.** A visit is one act of
  access by one provider. A row per message would bury the compliance signal
  under volume without adding a fact anyone would query for, and CLAUDE.md's
  consumer-auditing section states the rule directly: one row per unit of work.
  The transcript stream makes this more pointed rather than less — an encounter
  is hundreds of segments.
* **A refused connection writes no row.** No PHI was reached — the token was
  refused before the handshake completed and before any subscription existed.
  Failed authentication is an operational event and is logged as one, at WARNING.
* **The row is written on accept, before anything is relayed,** so a stream
  cannot be watched without the access being recorded. There is no request
  transaction to join, so the write goes on hipaa-logger's own pool.

``resource_type`` is ``Encounter`` rather than ``ClinicalNudge``: the access is to
one encounter's stream, not to a particular nudge row, and this service never
reads the ``clinical_nudges`` table — ``track-b-rag`` writes it and audits that
separately as ``WRITE_NUDGE``. ``resource_id`` is the session identifier, which is
what correlates these rows with track-a-clinical's ``START_SESSION`` row and with
audio-ingestion's ``STREAM_AUDIO`` row for the same visit.

**The two streams audit under two actions, and that is not bookkeeping.**
``RELAY_NUDGES`` means an encounter's alerts were streamed to a client;
``RELAY_TRANSCRIPT`` means its speech was. Those are different disclosures of
different data, and collapsing them into one action would make "was this
encounter's speech ever streamed to a client" unanswerable from the audit trail —
the same discipline that keeps ``STREAM_AUDIO``, ``WRITE_NUDGE`` and
``QUERY_POLICY`` apart.
"""

from __future__ import annotations

import uuid
from typing import Final

from hipaa_logger import AuditAction, audit_log

#: The vocabulary is defined once in hipaa-logger and imported, never
#: re-declared as a local string.
SERVICE_NAME: Final = "nudge-service"
RESOURCE_TYPE_ENCOUNTER: Final = "Encounter"


async def _audit_stream(
    action: AuditAction,
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    """Write one accepted-connection row under the given action.

    The only thing that varies between this service's two streams is the action;
    every other field is decided by the module docstring above and is identical
    for both. Keeping one writer means a change to the actor rule or the resource
    fields cannot be applied to one stream and forgotten on the other.
    """
    await audit_log(
        actor_id=str(provider_id),
        action=action,
        resource_type=RESOURCE_TYPE_ENCOUNTER,
        resource_id=str(session_id),
        session_id=str(session_id),
        service_name=SERVICE_NAME,
        ip_address=ip_address,
        user_agent=user_agent,
    )


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
    await _audit_stream(
        AuditAction.RELAY_NUDGES,
        session_id=session_id,
        provider_id=provider_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )


async def audit_transcript_stream(
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Record that one encounter's transcript stream was opened to a client.

    The arguments are the nudge stream's, for the same reasons; only the action
    differs. See the module docstring for why it is a different action rather than
    a second use of ``RELAY_NUDGES``.

    Args:
        session_id: The encounter's session identifier, from the validated token.
        provider_id: The provider the token was minted for, from the same token.
        ip_address: The connecting client's address, when the server reported one.
        user_agent: The connecting client's user agent, when it sent one.
    """
    await _audit_stream(
        AuditAction.RELAY_TRANSCRIPT,
        session_id=session_id,
        provider_id=provider_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )

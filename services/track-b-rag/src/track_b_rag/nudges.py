"""Turning one answered policy query into one nudge a provider actually sees.

This is the end of the path TASK-021 opened: a procedure is heard in a live
transcript, ``/policies/query`` answers what the payer requires and what this
encounter has not documented, and until now that answer was discarded. Here it
becomes a ``clinical_nudges`` row and a message on ``nudges:{session_id}``,
which TASK-041 relays to the browser and the phone.

**The payload shape is CLAUDE.md's, not this module's** — see "The nudge payload
— one shape". Five tasks read or write it and only this one writes it, so it is
fixed there and implemented here.

**Whether to nudge is not decided here either.** ``gap_analysis`` decides, by
returning a message or not returning one, and this module fires if and only if
it was given one. That indirection is the whole point: TASK-040 originally
specified a second trigger derived from ``missing_criteria`` and
``denial_risk``, and two derivations of one judgement is how a payer requiring
authorization with no published criteria came to compose a message that nothing
would ever show. Do not reintroduce a condition here.

**What this module does decide is escalation.** ``haptic`` is ``denial_risk ==
"high"`` *and* an answer that was actually established. A fallback is honestly
high risk — nothing verified it — but buzzing a physician's device once per
procedure across every concurrent encounter because Qdrant is unreachable
teaches them that the buzz means our vendor is down. That spends the
credibility the genuinely high-risk nudges depend on, so an outage alerts
without escalating.

**Order: store, then publish, and never a second row.** The payload carries the
row's ``nudge_id`` so TASK-041b's acknowledge has something real to update,
which fixes the order — a client must not be able to dismiss a nudge nobody
recorded. That order means a publish can fail with the row already written, and
the consumer then releases its dedup claim so the next mention of the procedure
tries again. The insert names the partial unique index from migration 0005 as an
``ON CONFLICT`` target and re-reads the existing row, so the retry republishes
the nudge that already exists rather than creating a twin of it.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Final

import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert as pg_insert

from track_a_clinical.models import ClinicalNudge
from track_b_rag import audit, db
from track_b_rag.api.schemas import PolicyQueryData
from track_b_rag.policy_dispatch import PolicyQueryParameters

logger = logging.getLogger(__name__)

#: The only nudge kind today. TASK-044 adds a second, which is why clients are
#: told to switch on this rather than assume it.
NUDGE_TYPE_PAYER_RULE_ALERT: Final = "PAYER_RULE_ALERT"

#: Matches migration 0005's partial unique index. Both the columns and the
#: predicate are needed: PostgreSQL cannot infer a partial index from the
#: column list alone.
_CONFLICT_COLUMNS: Final = ("encounter_id", "cpt_code")
_CONFLICT_PREDICATE: Final = "cpt_code IS NOT NULL"


def channel(session_id: uuid.UUID) -> str:
    """Return the nudge channel for one encounter, per CLAUDE.md's key list."""
    return f"nudges:{session_id}"


def should_escalate(answer: PolicyQueryData) -> bool:
    """Whether this nudge buzzes the device, as opposed to merely appearing.

    High risk *and* an answer we could actually establish. See the module
    docstring for why the fallback is excluded even though its risk is
    genuinely high — it is the one rule here that reads like a bug if the
    reason is not attached to it.
    """
    return answer.denial_risk == "high" and answer.source != "fallback"


def build_payload(
    *,
    nudge_id: uuid.UUID,
    parameters: PolicyQueryParameters,
    answer: PolicyQueryData,
) -> dict[str, Any]:
    """Assemble the message that goes on the wire.

    Carries the payer's criteria and the procedure, and nothing from the
    encounter's ``clinical_context``. A nudge crosses a WebSocket and is
    rendered in a browser; the provider already knows what is in their own note.
    """
    return {
        "type": NUDGE_TYPE_PAYER_RULE_ALERT,
        "nudge_id": str(nudge_id),
        "procedure": parameters.procedure,
        "cpt_code": parameters.cpt_code,
        "message": answer.nudge_message,
        "missing_criteria": list(answer.missing_criteria),
        "denial_risk": answer.denial_risk,
        "haptic": should_escalate(answer),
    }


async def _store(
    *,
    parameters: PolicyQueryParameters,
    answer: PolicyQueryData,
    session_id: uuid.UUID,
) -> tuple[uuid.UUID, bool]:
    """Write the nudge row, or find the one a previous attempt already wrote.

    Returns the row id and whether this call created it. The audit row is
    written on the same connection, inside the same transaction, and only when
    a row was actually inserted: a retry that finds an existing nudge is
    republishing something already recorded, not raising a second one.
    """
    statement = (
        pg_insert(ClinicalNudge)
        .values(
            encounter_id=parameters.encounter_id,
            procedure_name=parameters.procedure,
            cpt_code=parameters.cpt_code,
            nudge_message=answer.nudge_message,
            missing_criteria=list(answer.missing_criteria),
            denial_risk=answer.denial_risk,
            payer_policy_source=answer.policy_source,
        )
        .on_conflict_do_nothing(
            index_elements=list(_CONFLICT_COLUMNS),
            index_where=sa.text(_CONFLICT_PREDICATE),
        )
        .returning(ClinicalNudge.id)
    )

    async with db.get_sessionmaker()() as session, session.begin():
        inserted = (await session.execute(statement)).scalar_one_or_none()
        if inserted is None:
            existing = (
                await session.execute(
                    sa.select(ClinicalNudge.id).where(
                        ClinicalNudge.encounter_id == parameters.encounter_id,
                        ClinicalNudge.cpt_code == parameters.cpt_code,
                    )
                )
            ).scalar_one()
            return existing, False

        await audit.audit_nudge_write(
            nudge_id=inserted,
            session_id=session_id,
            provider_id=parameters.provider_id,
            conn=await db.raw_asyncpg_connection(session),
        )
        return inserted, True


async def emit(
    *,
    redis: Redis,
    session_id: uuid.UUID,
    parameters: PolicyQueryParameters,
    answer: PolicyQueryData,
) -> uuid.UUID | None:
    """Raise a nudge for one answered policy query, if there is one to raise.

    Args:
        redis: The client to publish on.
        session_id: The encounter, which names the channel.
        parameters: What the query was built from — the procedure, its code,
            the encounter row and the provider.
        answer: What ``/policies/query`` said.

    Returns:
        The nudge's id, or None when the answer had nothing worth interrupting
        the consultation for.

    Raises:
        Whatever the database or Redis raises. Deliberately not swallowed: the
        consumer releases its dedup claim on a failure here so the next mention
        of the procedure tries again, and a silently dropped nudge would
        suppress the procedure for the rest of the visit.
    """
    if answer.nudge_message is None:
        logger.debug(
            "No nudge for CPT %s in session %s — nothing to say",
            parameters.cpt_code,
            session_id,
        )
        return None

    nudge_id, created = await _store(parameters=parameters, answer=answer, session_id=session_id)
    payload = build_payload(nudge_id=nudge_id, parameters=parameters, answer=answer)
    await redis.publish(channel(session_id), json.dumps(payload))

    # Neither the message nor the criteria are logged. They name the payer's
    # requirements rather than the patient, but the procedure a given encounter
    # is being nudged about is close enough to clinical detail to keep out of
    # stdout, and the row already holds all of it.
    logger.info(
        "Nudge %s %s for CPT %s in session %s (risk=%s, haptic=%s)",
        nudge_id,
        "raised" if created else "republished",
        parameters.cpt_code,
        session_id,
        answer.denial_risk,
        should_escalate(answer),
    )
    return nudge_id

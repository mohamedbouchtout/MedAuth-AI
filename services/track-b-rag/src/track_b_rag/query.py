"""The two stages of a policy query, joined into one answer.

Stage 1 (:mod:`track_b_rag.policy_rules`) resolves what the payer requires and
is cached for a day. Stage 2 (:mod:`track_b_rag.gap_analysis`) compares those
requirements against this encounter's documentation and is never cached. This
module runs them in that order and assembles the response the route returns.

The split is why the expensive half — a vector search and a Sonnet call — is
paid for once per payer/plan/state/procedure per day while the patient-specific
half is recomputed on every request. It is also why a second query for the same
procedure and a different patient cannot inherit the first patient's answer.

When Stage 1 falls back, Stage 2 does not run at all. The fallback means the
payer's criteria are unknown, and there is nothing to compare a note against; a
computed ``missing_criteria`` of ``[]`` would then read as "nothing is missing",
which is precisely the false reassurance TASK-012's fallback exists to prevent.
The response in that case is the fixed one that task specifies, verbatim.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from qdrant_client import QdrantClient
from redis.asyncio import Redis

from track_b_rag import gap_analysis, policy_rules
from track_b_rag.gap_analysis import DenialRisk
from track_b_rag.policy_rules import RulesSource

logger = logging.getLogger(__name__)

#: Word for word from TASK-012. It is what a provider reads when this service
#: could not establish the payer's requirements, so it says exactly that and
#: asks for a manual check rather than implying an answer either way.
FALLBACK_NUDGE_MESSAGE: Final = "Unable to verify authorization requirements — confirm manually"


@dataclass(frozen=True)
class PolicyQueryAnswer:
    """One answered policy query: the payer's rules and this encounter's gaps.

    ``source`` is not part of the HTTP response. It records which path answered
    — cache, RAG, or the safe fallback — for the log line and for the tests that
    assert a cache hit did not reach Bedrock.
    """

    requires_auth: bool
    auth_criteria: list[str]
    missing_criteria: list[str]
    denial_risk: DenialRisk
    nudge_message: str
    step_therapy_required: bool
    step_therapy_details: str | None
    source: RulesSource = field(default="rag")


def fallback_answer() -> PolicyQueryAnswer:
    """Return the safe answer for a query that could not be resolved.

    Authorization assumed required, no criteria claimed, denial risk high. A
    function rather than a module-level constant so each caller gets its own
    lists and nothing downstream can mutate the shared default.
    """
    return PolicyQueryAnswer(
        requires_auth=True,
        auth_criteria=[],
        missing_criteria=[],
        denial_risk="high",
        nudge_message=FALLBACK_NUDGE_MESSAGE,
        step_therapy_required=False,
        step_therapy_details=None,
        source="fallback",
    )


async def answer_policy_query(
    *,
    qdrant: QdrantClient,
    redis: Redis,
    collection: str,
    procedure: str,
    cpt_code: str,
    payer: str,
    plan_type: str,
    state: str,
    clinical_context: Mapping[str, Any],
) -> PolicyQueryAnswer:
    """Answer one policy query for one encounter.

    Args:
        qdrant: Client for the policy collection.
        redis: Cache client for the Stage 1 result.
        collection: Qdrant collection holding the indexed policy chunks.
        procedure: The procedure as the clinician described it.
        cpt_code: The authoritative procedure code.
        payer: Issuing payer, as ingested.
        plan_type: Plan type, e.g. ``PPO``.
        state: Two-letter state code.
        clinical_context: What this encounter has documented so far. Reaches
            Stage 2 only — it is never embedded, never sent to Bedrock and never
            written to the cache.

    Returns:
        The combined answer, or the fixed fallback when the payer's rules could
        not be established.
    """
    resolution = await policy_rules.resolve_policy_rules(
        qdrant=qdrant,
        redis=redis,
        collection=collection,
        procedure=procedure,
        cpt_code=cpt_code,
        payer=payer,
        plan_type=plan_type,
        state=state,
    )

    if resolution.is_fallback:
        logger.warning(
            "Answering %s/%s/%s CPT %s with the manual-review fallback",
            payer,
            plan_type,
            state,
            cpt_code,
        )
        return fallback_answer()

    rules = resolution.rules
    assessment = gap_analysis.assess(
        rules=rules,
        clinical_context=clinical_context,
        procedure=procedure,
    )

    logger.info(
        "Answered %s/%s/%s CPT %s from %s: auth=%s, %d of %d criteria undocumented, risk=%s",
        payer,
        plan_type,
        state,
        cpt_code,
        resolution.source,
        rules.requires_auth,
        len(assessment.missing_criteria),
        len(rules.auth_criteria),
        assessment.denial_risk,
    )

    return PolicyQueryAnswer(
        requires_auth=rules.requires_auth,
        auth_criteria=list(rules.auth_criteria),
        missing_criteria=assessment.missing_criteria,
        denial_risk=assessment.denial_risk,
        nudge_message=assessment.nudge_message,
        step_therapy_required=rules.step_therapy_required,
        step_therapy_details=rules.step_therapy_details,
        source=resolution.source,
    )

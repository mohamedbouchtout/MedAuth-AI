"""``POST /policies/query`` — the endpoint the clinical nudges are built on.

Given a procedure and what an encounter has documented so far, this answers
whether the payer requires prior authorization, what it requires to be
documented, what is still missing, and what to tell the provider. TASK-021's
transcript scanner calls it the moment a procedure keyword is heard, and
TASK-040 turns the answer into a nudge.

**This route touches PHI and therefore audits**, which is the one thing
separating it from ``/policies/ingest`` in the module next door: the request
carries a live encounter's clinical context. Known Constraints #6 binds in both
directions, so the ingest route must not write an audit row and this one must.
The row is written before any work begins — the access has already happened by
the time the body is parsed, and hipaa-logger never suppresses a failed write,
so auditing first means an unrecordable access does not proceed.

The answer is assembled in two stages that are deliberately not cached alike;
:mod:`track_b_rag.query` holds that reasoning. Nothing about which stage came
from where reaches the response, and no failure of the underlying path reaches
the caller as a 5xx: an unresolvable query returns the safe
"confirm manually" answer TASK-012 specifies, because a consumer firing nudges
during a live encounter reads an error as silence, and silence reads as
"nothing to worry about".
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from qdrant_client import QdrantClient
from redis.asyncio import Redis

from api_envelope import ApiResponse, error_responses
from payer_vocab import is_known_payer, normalize_payer
from track_b_rag import audit, query
from track_b_rag.api.dependencies import get_qdrant, get_redis
from track_b_rag.api.schemas import PolicyQueryData, PolicyQueryRequest
from track_b_rag.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/policies", tags=["policies"])

#: What a 422 means on this route specifically. The generic wording does not
#: say which half of the body a caller usually gets wrong.
QUERY_ERROR_DESCRIPTIONS = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: (
        "The request body is invalid — a missing field, a malformed state code, "
        "or a session_id or provider_id that is not a UUID."
    ),
}


def _client_ip(request: Request) -> str | None:
    """Return the requesting client's IP, or None when the transport has no peer."""
    return request.client.host if request.client else None


def _resolve_payer(raw: str) -> str:
    """Return the canonical slug for a payer name, logging one we do not recognise.

    An unfamiliar payer is not an error — it queries, retrieves nothing we have
    indexed for it, and gets the safe fallback answer, which is honest. What is
    *not* honest is leaving that outcome indistinguishable from a payer whose
    policies we hold but whose name failed to line up, which is what happened
    before TASK-016: the caller sees "no policy found" either way. The WARNING is
    the only thing separating the two after the fact, so it names both spellings.
    """
    slug = normalize_payer(raw)
    if not is_known_payer(slug):
        logger.warning(
            "Policy query for unrecognised payer %r (normalised to %r) — "
            "retrieval will only match documents ingested under that same slug",
            raw,
            slug,
        )
    return slug


@router.post(
    "/query",
    response_model=ApiResponse[PolicyQueryData],
    summary="Query prior authorization requirements",
    response_description="The payer's requirements and this encounter's gaps against them.",
    responses=error_responses(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=QUERY_ERROR_DESCRIPTIONS,
    ),
)
async def query_policies(
    body: PolicyQueryRequest,
    request: Request,
    client: Annotated[QdrantClient, Depends(get_qdrant)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ApiResponse[PolicyQueryData]:
    """Answer whether a procedure needs prior authorization, and what is missing.

    Resolves the payer's rules for the CPT code — from the Redis cache when one
    is current, otherwise by searching the indexed policy text and analysing it
    with Claude Sonnet on Bedrock — then compares those rules against this
    encounter's `clinical_context` to produce the gaps, the denial risk and the
    nudge text. Only the payer half is cached; the comparison runs on every
    call, so two patients with the same plan and procedure get the same criteria
    and their own gaps.

    Returns 200 with the full answer. When the payer's rules cannot be
    established at all, the answer is the safe default — authorization assumed
    required, denial risk high, and a message asking for a manual check — rather
    than an error.

    Writes an `audit_log` row: this endpoint reads a live encounter's clinical
    context, which is PHI. The row names the provider and the session and
    nothing about the clinical detail itself.

    Internal service-to-service endpoint, called by TASK-021's transcript
    consumer. Like `/policies/ingest`, access control is network isolation.
    """
    await audit.audit_policy_query(
        session_id=body.session_id,
        provider_id=body.provider_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    answer = await query.answer_policy_query(
        qdrant=client,
        redis=redis,
        collection=get_settings().qdrant_collection,
        procedure=body.procedure,
        cpt_code=body.cpt_code,
        payer=_resolve_payer(body.payer),
        plan_type=body.plan_type,
        state=body.state,
        clinical_context=body.clinical_context,
    )

    return ApiResponse[PolicyQueryData](
        data=PolicyQueryData(
            requires_auth=answer.requires_auth,
            auth_criteria=answer.auth_criteria,
            missing_criteria=answer.missing_criteria,
            denial_risk=answer.denial_risk,
            nudge_message=answer.nudge_message,
            step_therapy_required=answer.step_therapy_required,
            step_therapy_details=answer.step_therapy_details,
            source=answer.source,
        )
    )

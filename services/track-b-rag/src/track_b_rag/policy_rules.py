"""Stage 1 — what the payer requires for a procedure, independent of any patient.

This is the cacheable, expensive half of ``/policies/query``: a Qdrant search
over indexed policy text followed by one Sonnet call over what came back. Its
answer describes a payer's rules for a CPT code and would be identical for every
patient with that payer, plan type, state and procedure, which is what makes it
safe to keep in Redis for a day.

**No patient data enters this module.** :func:`resolve_policy_rules` takes no
clinical context, by construction rather than by convention — nothing
patient-specific can reach the prompt, the retrieved passages or the cached
value, because the function has no parameter to carry it. The comparison against
this encounter's documentation is Stage 2, in :mod:`track_b_rag.gap_analysis`,
and it runs fresh on every call.

:func:`resolve_policy_rules` is also where TASK-015's Da Vinci CRD tier joins.
For a payer covered by the CMS-0057-F mandate, the payer's own CRD endpoint
answers *whether* prior authorization is required and that answer overrides the
one reasoned out of policy text. It does not replace the rest: CRD carries no
itemised criteria — the IG delegates those to a DTR Questionnaire — so
``auth_criteria`` and the step therapy fields still come from the RAG path
below, and Stage 2 keeps working. See :mod:`track_b_rag.crd` for how that was
established against the real Reference Implementation and the IG.

The two tiers run concurrently, because they need nothing from each other and a
nudge is read mid-encounter. The CRD answer is applied *after* the RAG result is
written to the cache, so what Redis holds is always the payer-policy answer and
never a live determination — see CLAUDE.md, "A CRD answer is never cached; a RAG
answer is." For the commercial plans the mandate does not cover, and on any CRD
failure, this module behaves exactly as it did before TASK-015. Callers see one
function and one return type either way.

Every failure of the RAG path lands on the same safe fallback: authorization
required, no criteria known, and a message telling the provider to confirm
manually. TASK-012 specifies that for an answer that will not parse; this module
applies it to an unreachable vector store, a Bedrock error, and a retrieval that
matched nothing, on the same reasoning. The alternative is a 5xx to a consumer
(TASK-021) that fires nudges during a live encounter, where silence reads as
"nothing to worry about" — the one conclusion this service must never imply by
accident. A fallback is never cached: it says what this call failed to learn,
not what the payer requires.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from qdrant_client import QdrantClient
from redis.asyncio import Redis
from starlette.concurrency import run_in_threadpool

from bedrock_client import first_json_object
from track_b_rag import bedrock, cache, crd, retrieval
from track_b_rag.config import get_settings

logger = logging.getLogger(__name__)

#: One retry, per TASK-012 — the same prompt, on the theory that a malformed
#: answer is a sampling accident rather than a prompt defect. A second failure
#: is treated as a failure of the path, not something to keep paying for.
MAX_ATTEMPTS: Final = 2

#: Where an answer came from. The ``crd`` prefix means the payer's own CRD
#: endpoint decided ``requires_auth``; what follows it is where the criteria
#: came from. Bare ``crd`` means CRD answered and the RAG path could not, so the
#: criteria list is empty. TASK-015 requires this to be reported explicitly:
#: with CRD results deliberately uncached, "the CRD tier answered" can no longer
#: be inferred from cache state, so a test asserts it directly instead.
RulesSource = Literal["cache", "rag", "fallback", "crd", "crd+cache", "crd+rag"]


class PolicyRules(BaseModel):
    """The payer's rules for one procedure. Cacheable across patients.

    ``extra="ignore"`` rather than ``forbid``: an answer carrying the four
    fields plus a chatty ``notes`` key is a usable answer, and spending the
    single retry on it would trade a correct result for a fallback.
    """

    model_config = ConfigDict(extra="ignore")

    requires_auth: bool = Field(
        description="Whether the payer requires prior authorization for this procedure.",
    )
    auth_criteria: list[str] = Field(
        default_factory=list,
        description="What the payer requires to be documented before it will authorize.",
    )
    step_therapy_required: bool = Field(
        default=False,
        description="Whether the plan requires a first-line therapy to be tried first.",
    )
    step_therapy_details: str | None = Field(
        default=None,
        description="What the step therapy requirement is, when there is one.",
    )
    policy_source: str | None = Field(
        default=None,
        description=(
            "Which indexed policy documents these criteria were read from. Set "
            "from the retrieved chunks, never from the model's answer."
        ),
    )

    @field_validator("auth_criteria", mode="after")
    @classmethod
    def _drop_blank_criteria(cls, value: list[str]) -> list[str]:
        """Discard empty criteria, which Stage 2 could only ever report as missing."""
        return [criterion.strip() for criterion in value if criterion.strip()]

    @field_validator("step_therapy_details", mode="after")
    @classmethod
    def _blank_details_are_absent(cls, value: str | None) -> str | None:
        """Treat an empty string as no detail, so callers test one thing and not two."""
        if value is None or not value.strip():
            return None
        return value.strip()


#: How a policy-tier provenance reads once CRD has supplied ``requires_auth``.
#: A mapping rather than an f-string so the composed value stays inside
#: :data:`RulesSource` — a source that cannot be spelled here is one the policy
#: tier does not produce.
_CRD_SOURCES: Final[dict[RulesSource, RulesSource]] = {
    "cache": "crd+cache",
    "rag": "crd+rag",
    "fallback": "crd",
    "crd": "crd",
    "crd+cache": "crd+cache",
    "crd+rag": "crd+rag",
}

#: What Stage 1 reports when it could not find out: authorization required and
#: nothing known about the criteria. Failing toward "flag for manual review" is
#: TASK-012's stated direction — the opposite failure, assuming no authorization
#: is needed, is the one that costs a patient a denied claim.
FALLBACK_RULES: Final = PolicyRules(
    requires_auth=True,
    auth_criteria=[],
    step_therapy_required=False,
    step_therapy_details=None,
)

PROMPT_TEMPLATE = """\
You are analyzing health insurance prior authorization policy text for a \
clinical decision support system.

Payer: {payer}
Plan type: {plan_type}
State: {state}
Procedure code (authoritative): CPT {cpt_code}
Procedure as described by the ordering clinician: {procedure}

Answer for the CPT code. Treat the clinician's wording as a hint about intent \
only; where the two disagree, the code decides what is being asked about.

Below are passages retrieved from that payer's published policy documents. Use \
only these passages. If they do not establish an answer, say authorization is \
required and return an empty criteria list rather than guessing.

{passages}

Return a single JSON object and nothing else — no prose, no markdown fence. \
Its shape:

{{
  "requires_auth": boolean,
  "auth_criteria": [string, ...],
  "step_therapy_required": boolean,
  "step_therapy_details": string or null
}}

"auth_criteria" are the clinical facts the payer requires to be documented \
before it will authorize, one per string, each phrased so that it can be \
checked against a clinical note. "step_therapy_details" is null unless \
"step_therapy_required" is true.
"""


@dataclass(frozen=True)
class PolicyRulesResolution:
    """Stage 1's answer, and where it came from.

    ``source`` is what the tests assert on and what the route logs: ``cache``
    means no Bedrock call was made, ``rag`` means the full path ran, and
    ``fallback`` means it failed and the answer is the safe default. A ``crd``
    prefix means the payer's own CRD endpoint supplied ``requires_auth``; bare
    ``crd`` means it did so where the policy tier had nothing.
    """

    rules: PolicyRules
    source: RulesSource

    @property
    def is_fallback(self) -> bool:
        """Whether this is the safe default rather than a real answer."""
        return self.source == "fallback"


async def resolve_policy_rules(
    *,
    qdrant: QdrantClient,
    redis: Redis,
    collection: str,
    procedure: str,
    cpt_code: str,
    payer: str,
    plan_type: str,
    state: str,
) -> PolicyRulesResolution:
    """Return the payer's rules for a procedure, from cache when possible.

    Runs two tiers concurrently: the payer's own Da Vinci CRD endpoint, where
    the CMS-0057-F mandate covers it, and the cached RAG path over indexed
    policy text. CRD decides ``requires_auth`` when it answers; everything else
    comes from the policy text either way. Note the absence of a clinical
    context parameter: Stage 1 cannot see the patient, so what it produces is
    safe to share between them — and that is also why the CRD request carries no
    demographics.

    Args:
        qdrant: Client for the policy collection.
        redis: Cache client. An unreachable one costs money, never correctness.
        collection: Qdrant collection holding the indexed policy chunks.
        procedure: The procedure as the clinician described it.
        cpt_code: The authoritative procedure code.
        payer: Canonical payer slug — what the Qdrant filter matches, the cache
            key is built from, and the CRD support table is checked against.
        plan_type: Plan type, e.g. ``PPO``.
        state: Two-letter state code.

    Returns:
        The rules and their provenance. A ``fallback`` resolution is never
        written to the cache, and neither is a CRD determination.
    """
    policy_tier, determination = await asyncio.gather(
        _resolve_policy_tier(
            qdrant=qdrant,
            redis=redis,
            collection=collection,
            procedure=procedure,
            cpt_code=cpt_code,
            payer=payer,
            plan_type=plan_type,
            state=state,
        ),
        _crd_determination(
            procedure=procedure,
            cpt_code=cpt_code,
            payer=payer,
            plan_type=plan_type,
            state=state,
        ),
    )
    if determination is None:
        return policy_tier
    return _apply_determination(policy_tier, determination)


async def _crd_determination(
    *,
    procedure: str,
    cpt_code: str,
    payer: str,
    plan_type: str,
    state: str,
) -> crd.CrdDetermination | None:
    """Return the payer's own authorization determination, if there is one.

    None for a payer outside the CMS-0057-F mandate, for a CRD endpoint that is
    not configured, and for any call that failed or decided nothing. The tier
    can only add an answer, never remove one.
    """
    if not crd.is_crd_supported(payer):
        return None
    base_url = get_settings().crd_base_url
    if not base_url:
        return None
    return await crd.determine(
        base_url=base_url,
        timeout_seconds=get_settings().crd_timeout_seconds,
        procedure=procedure,
        cpt_code=cpt_code,
        payer=payer,
        plan_type=plan_type,
        state=state,
    )


def _apply_determination(
    resolution: PolicyRulesResolution,
    determination: crd.CrdDetermination,
) -> PolicyRulesResolution:
    """Return `resolution` with the payer's own authorization answer applied.

    The criteria, and the step therapy fields, are left exactly as the policy
    tier produced them — CRD does not carry any of them. Only ``requires_auth``
    is replaced, because that is the single field the payer has stated directly
    rather than published in prose for us to interpret.

    A policy tier that fell back becomes a real answer rather than the safe
    default: the payer has told us whether authorization is required, which is
    more than the fallback claims to know. The criteria list stays empty, so
    Stage 2 reports nothing missing and the nudge says the criteria could not be
    found — which is precisely the situation.
    """
    if resolution.is_fallback:
        rules = PolicyRules(requires_auth=determination.requires_auth)
        return PolicyRulesResolution(rules=rules, source="crd")
    rules = resolution.rules.model_copy(update={"requires_auth": determination.requires_auth})
    return PolicyRulesResolution(rules=rules, source=_CRD_SOURCES[resolution.source])


async def _resolve_policy_tier(
    *,
    qdrant: QdrantClient,
    redis: Redis,
    collection: str,
    procedure: str,
    cpt_code: str,
    payer: str,
    plan_type: str,
    state: str,
) -> PolicyRulesResolution:
    """Return the payer's rules as read from indexed policy text, or the fallback.

    This is TASK-012's Stage 1 unchanged: the Redis cache in front of a Qdrant
    search and one Sonnet call. It is a separate function from
    :func:`resolve_policy_rules` so that the cache write below happens on what
    the policy text says and nothing else — a CRD determination is applied to
    the returned value afterwards and therefore cannot reach Redis.
    """
    key = cache.policy_rules_key(
        payer=payer,
        plan_type=plan_type,
        state=state,
        cpt_code=cpt_code,
    )

    cached = await cache.get_cached(redis, key)
    if cached is not None:
        cached_rules = _parse_cached(cached, key)
        if cached_rules is not None:
            logger.debug("Policy rules for %s served from cache", key)
            return PolicyRulesResolution(rules=cached_rules, source="cache")

    rules = await _resolve_from_rag(
        qdrant=qdrant,
        collection=collection,
        procedure=procedure,
        cpt_code=cpt_code,
        payer=payer,
        plan_type=plan_type,
        state=state,
    )
    if rules is None:
        return PolicyRulesResolution(rules=FALLBACK_RULES, source="fallback")

    await cache.set_cached(
        redis,
        key,
        rules.model_dump_json(),
        cache.POLICY_RULES_TTL_SECONDS,
    )
    return PolicyRulesResolution(rules=rules, source="rag")


def _parse_cached(raw: str, key: str) -> PolicyRules | None:
    """Return the cached rules, or None if the entry is not usable.

    An entry written by an older version of this model, or corrupted, is treated
    as a miss rather than an error: recomputing is always available, and a cache
    that can poison a request is worse than no cache.
    """
    try:
        return PolicyRules.model_validate_json(raw)
    except ValueError:
        logger.warning("Discarding unreadable cache entry at %r", key, exc_info=True)
        return None


async def _resolve_from_rag(
    *,
    qdrant: QdrantClient,
    collection: str,
    procedure: str,
    cpt_code: str,
    payer: str,
    plan_type: str,
    state: str,
) -> PolicyRules | None:
    """Run retrieval and the model, returning None if the path could not answer.

    Every failure mode collapses to None, which the caller turns into the safe
    fallback: nothing indexed for this payer, an unreachable Qdrant, a Bedrock
    error, an answer that is not JSON, and an answer that is JSON of the wrong
    shape. Each is logged with the payer, procedure and code — never with
    anything about a patient, which this function has no access to anyway.
    """
    context = f"{payer}/{plan_type}/{state} CPT {cpt_code} ({procedure})"

    try:
        chunks = await run_in_threadpool(
            retrieval.retrieve,
            qdrant,
            collection=collection,
            procedure=procedure,
            cpt_code=cpt_code,
            payer=payer,
            plan_type=plan_type,
            state=state,
        )
    except Exception:
        logger.error("Policy retrieval failed for %s", context, exc_info=True)
        return None

    if not chunks:
        logger.warning(
            "No indexed policy text matched %s; answering with the safe fallback",
            context,
        )
        return None

    prompt = build_prompt(
        procedure=procedure,
        cpt_code=cpt_code,
        payer=payer,
        plan_type=plan_type,
        state=state,
        chunks=chunks,
    )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            answer = await bedrock.invoke_reasoning(prompt)
        except Exception:
            logger.error(
                "Bedrock call failed for %s (attempt %d of %d)",
                context,
                attempt,
                MAX_ATTEMPTS,
                exc_info=True,
            )
            continue

        rules = parse_rules(answer)
        if rules is not None:
            # Overwritten unconditionally, so an answer that named its own
            # sources cannot keep them. See :func:`policy_source`.
            return rules.model_copy(update={"policy_source": policy_source(chunks)})

        logger.warning(
            "Bedrock returned an unusable policy analysis for %s (attempt %d of %d)",
            context,
            attempt,
            MAX_ATTEMPTS,
        )

    logger.error("Falling back to manual review for %s after %d attempts", context, MAX_ATTEMPTS)
    return None


#: Width of ``clinical_nudges.payer_policy_source``. The provenance string is
#: built to fit the column it ends up in rather than being truncated on write,
#: where a mid-identifier cut would produce a policy id that looks real and
#: refers to nothing.
POLICY_SOURCE_MAX_LENGTH: Final = 500


def policy_source(chunks: Sequence[retrieval.RetrievedChunk]) -> str | None:
    """Return the policies these criteria were read from, or None if none were.

    Distinct policy ids in retrieval rank order, comma-separated. Ranked rather
    than sorted because if the string has to be shortened, the passages that
    contributed least are the ones to lose.

    This is derived from what was retrieved and is never taken from the model's
    answer. A model asked to name its own sources will supply plausible ones,
    and a fabricated citation on a nudge is worse than no citation: it is the
    field a reviewer would use to check whether the criteria were real.
    """
    ordered: list[str] = []
    for chunk in chunks:
        if chunk.policy_id and chunk.policy_id not in ordered:
            ordered.append(chunk.policy_id)

    while ordered:
        joined = ",".join(ordered)
        if len(joined) <= POLICY_SOURCE_MAX_LENGTH:
            return joined
        ordered.pop()
    return None


def build_prompt(
    *,
    procedure: str,
    cpt_code: str,
    payer: str,
    plan_type: str,
    state: str,
    chunks: list[retrieval.RetrievedChunk],
) -> str:
    """Return the Sonnet prompt for one policy question.

    Each passage is labelled with the policy it came from, so the model can tell
    two documents apart when a payer publishes both a general and a
    state-specific policy for the same procedure.
    """
    passages = "\n\n".join(
        f"[Passage {index}, policy {chunk.policy_id}]\n{chunk.text}"
        for index, chunk in enumerate(chunks, start=1)
    )
    return PROMPT_TEMPLATE.format(
        payer=payer,
        plan_type=plan_type,
        state=state,
        cpt_code=cpt_code,
        procedure=procedure,
        passages=passages,
    )


def parse_rules(answer: str) -> PolicyRules | None:
    """Return the rules described by a model answer, or None if it is unusable.

    Tolerates the two things a model does to JSON it was asked to return bare —
    wrapping it in a markdown fence, and prefacing it with a sentence — by
    locating the first balanced object in the text. It does not tolerate a
    document of the wrong shape: that is what the retry and then the fallback
    exist for.
    """
    document = first_json_object(answer)
    if document is None:
        return None
    try:
        parsed: Any = json.loads(document)
    except ValueError:
        return None
    try:
        return PolicyRules.model_validate(parsed)
    except ValueError:
        # Covers both a document of the wrong shape and one that is not an
        # object at all — model_validate raises a ValidationError, which is a
        # ValueError, for either.
        return None

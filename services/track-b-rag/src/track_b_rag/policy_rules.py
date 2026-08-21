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

:func:`resolve_policy_rules` is also the seam TASK-015 puts the Da Vinci CRD
path in front of. For a payer covered by the CMS-0057-F mandate, CRD answers
these same four fields from a standardised API and the RAG path below is
skipped; on any CRD failure, or for the commercial plans the mandate does not
cover, this implementation runs unchanged. Callers see one function and one
return type either way.

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

import json
import logging
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from qdrant_client import QdrantClient
from redis.asyncio import Redis
from starlette.concurrency import run_in_threadpool

from track_b_rag import bedrock, cache, retrieval

logger = logging.getLogger(__name__)

#: One retry, per TASK-012 — the same prompt, on the theory that a malformed
#: answer is a sampling accident rather than a prompt defect. A second failure
#: is treated as a failure of the path, not something to keep paying for.
MAX_ATTEMPTS: Final = 2

RulesSource = Literal["cache", "rag", "fallback"]


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
    ``fallback`` means it failed and the answer is the safe default.
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

    The seam TASK-015 fronts with Da Vinci CRD. Note the absence of a clinical
    context parameter: Stage 1 cannot see the patient, so what it produces is
    safe to share between them.

    Args:
        qdrant: Client for the policy collection.
        redis: Cache client. An unreachable one costs money, never correctness.
        collection: Qdrant collection holding the indexed policy chunks.
        procedure: The procedure as the clinician described it.
        cpt_code: The authoritative procedure code.
        payer: Issuing payer, as ingested.
        plan_type: Plan type, e.g. ``PPO``.
        state: Two-letter state code.

    Returns:
        The rules and their provenance. A ``fallback`` resolution is never
        written to the cache.
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
            return rules

        logger.warning(
            "Bedrock returned an unusable policy analysis for %s (attempt %d of %d)",
            context,
            attempt,
            MAX_ATTEMPTS,
        )

    logger.error("Falling back to manual review for %s after %d attempts", context, MAX_ATTEMPTS)
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
    document = _first_json_object(answer)
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


def _first_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` span in `text`, or None if there is none.

    Brace counting rather than a regular expression, because criteria strings can
    contain braces and quotes; the scan tracks string literals and their escapes,
    so a brace inside one does not close the object.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None

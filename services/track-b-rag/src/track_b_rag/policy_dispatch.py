"""The seam between a detected procedure and ``POST /policies/query``.

The transcript consumer knows a procedure was mentioned, in which session, and
what was said around it. ``/policies/query`` needs rather more than that:
``procedure``, ``cpt_code``, ``payer``, ``plan_type``, ``state``,
``clinical_context``, ``session_id`` and ``provider_id``. This module is where
the first list becomes the second, and it is deliberately split so that the part
which cannot be built yet is one named function rather than a hole spread
through the consumer.

**What is not built yet, and why it is not stubbed with placeholder values.**
Four of those fields have no source in the system today:

* ``state`` — the ``encounters`` table has no state column at all. Adding one is
  a migration against schema owned by track-a-clinical (TASK-005), which is a
  reviewed change and not a side effect of this task.
* ``cpt_code`` — nothing maps a spoken keyword such as "MRI" onto a procedure
  code. That mapping is a piece of clinical data modelling with its own design
  questions (which of the dozens of MRI codes; how a new specialty extends it).
* ``payer`` and ``plan_type`` — the columns exist on ``encounters`` but are
  populated from a FHIR ``Coverage`` resource at SMART launch, which is Phase 5.

A placeholder for ``cpt_code`` would be worse than no answer at all. The Redis
cache key is ``rag:{payer}:{plan_type}:{state}:{cpt_code}``, so a made-up code
writes a real policy answer under a key that stands for a different procedure,
and two unrelated procedures then collide on it. That is a wrong answer served
confidently to the next encounter, which is strictly worse than the silence of
not querying. TASK-024 closes this; :func:`resolve_query_parameters` raises
until it does.

**The query goes over HTTP even though the route is in this same process.** The
``audit_log()`` write for ``/policies/query`` lives in the route layer, because
what has to be recorded is that a PHI-carrying request was made on behalf of a
particular provider for a particular session. Calling
``query.answer_policy_query()`` directly from here would skip it, and moving the
audit down into that function so both paths were covered would put the
compliance obligation in two places. Two hand-maintained copies of an obligation
drift; one call path does not. So this posts to the route like any other caller.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from track_b_rag.api.schemas import PolicyQueryData
from track_b_rag.config import get_settings
from track_b_rag.keywords import ProcedureMention

logger = logging.getLogger(__name__)

POLICY_QUERY_PATH: Final = "/policies/query"


@dataclass(frozen=True)
class PolicyQueryParameters:
    """Everything ``POST /policies/query`` needs that a transcript does not carry.

    Attributes:
        procedure: The procedure as the clinician described it.
        cpt_code: The authoritative procedure code.
        payer: The payer, in any spelling — the route normalises it to the
            canonical slug from ``packages/payer-vocab``.
        plan_type: Plan type, e.g. ``PPO``.
        state: Two-letter state code for the encounter.
        provider_id: The provider the query is made on behalf of, for the audit
            row the route writes.
    """

    procedure: str
    cpt_code: str
    payer: str
    plan_type: str
    state: str
    provider_id: uuid.UUID


class MissingQueryParameters(Exception):
    """Raised when a policy query cannot be built for a detected procedure.

    This is a structural failure, not a transient one: retrying the same mention
    a second later fails identically. The consumer treats it that way — it logs
    once per procedure per session and keeps the dedup claim, rather than
    repeating the same warning for every segment that names the procedure again.

    Attributes:
        fields: The parameters that could not be supplied, so the log line says
            what is actually missing instead of "could not build a query".
    """

    def __init__(self, fields: tuple[str, ...]) -> None:
        self.fields = fields
        super().__init__(f"No source yet for: {', '.join(fields)}")


#: What :func:`resolve_query_parameters` cannot supply. Named as data rather
#: than buried in the raise so the consumer's tests, and anyone reading the
#: warning in a log, can see the list without reading the function.
UNRESOLVED_PARAMETERS: Final[tuple[str, ...]] = (
    "payer",
    "plan_type",
    "state",
    "cpt_code",
    "provider_id",
)


async def resolve_query_parameters(
    *,
    session_id: uuid.UUID,
    mention: ProcedureMention,
) -> PolicyQueryParameters:
    """Resolve the query parameters for a detected procedure.

    Args:
        session_id: The encounter the mention belongs to.
        mention: The detected procedure.

    Returns:
        The parameters for ``POST /policies/query``.

    Raises:
        MissingQueryParameters: Always, for now. See the module docstring: the
            ``state`` column does not exist, no keyword-to-CPT mapping exists,
            and the payer columns are not populated until Phase 5. **TASK-024**
            replaces this body; nothing else in this module or in the consumer
            has to change when it does, which is the reason the seam is a
            function rather than an inline lookup.
    """
    del session_id, mention  # Named for the signature TASK-024 implements.
    raise MissingQueryParameters(UNRESOLVED_PARAMETERS)


async def post_policy_query(
    *,
    parameters: PolicyQueryParameters,
    session_id: uuid.UUID,
    clinical_context: Mapping[str, Any],
    base_url: str | None = None,
    timeout_seconds: float | None = None,
) -> PolicyQueryData | None:
    """Ask ``POST /policies/query`` about one procedure.

    Args:
        parameters: The resolved query parameters.
        session_id: The encounter, for the route's audit row.
        clinical_context: What this encounter has documented so far, as
            extracted from the transcript. PHI: it is sent, and never logged.
        base_url: Override for the service's own base URL; defaults to the
            configured one.
        timeout_seconds: Override for the request timeout.

    Returns:
        The parsed answer, or None when the call failed. Every failure mode —
        timeout, transport error, non-2xx, a body that does not match the
        response model — returns None and logs at ERROR. The route itself has no
        5xx path by design, so a failure here means the request never arrived or
        never came back, not that the policy question was answered badly.

    Note the log line carries payer, plan and code and never the clinical
    context, matching what the route's own error paths are allowed to say.
    """
    settings = get_settings()
    url = f"{(base_url or settings.policy_query_base_url).rstrip('/')}{POLICY_QUERY_PATH}"
    context = f"{parameters.payer}/{parameters.plan_type}/{parameters.state} "
    context += f"CPT {parameters.cpt_code}"

    body = {
        "procedure": parameters.procedure,
        "cpt_code": parameters.cpt_code,
        "payer": parameters.payer,
        "plan_type": parameters.plan_type,
        "state": parameters.state,
        "clinical_context": dict(clinical_context),
        "session_id": str(session_id),
        "provider_id": str(parameters.provider_id),
    }

    try:
        timeout = timeout_seconds or settings.policy_query_timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=body)
            response.raise_for_status()
            payload = response.json()
        return PolicyQueryData.model_validate(payload["data"])
    except Exception:
        # ERROR rather than the WARNING the CRD tier uses: a failed CRD lookup
        # still leaves the provider with an answer, and a failed policy query
        # leaves them with nothing at all for a procedure that was ordered.
        logger.error("Policy query failed for %s", context, exc_info=True)
        return None


async def resolve_and_query_policy(
    *,
    session_id: uuid.UUID,
    mention: ProcedureMention,
    clinical_context: Mapping[str, Any],
) -> PolicyQueryData | None:
    """Turn one detected procedure into one policy query.

    The single entry point the transcript consumer calls, so that everything the
    consumer needs from the query side is one function with one signature.

    Args:
        session_id: The encounter the mention belongs to.
        mention: The detected procedure.
        clinical_context: The transcript excerpt and its metadata.

    Returns:
        The answer, or None when the query could not be completed.

    Raises:
        MissingQueryParameters: When the query cannot be built at all, which is
            every call until TASK-024. Deliberately not swallowed here: the
            consumer distinguishes "this can never work" from "this did not work
            this time" and handles the dedup claim differently for each.
    """
    parameters = await resolve_query_parameters(session_id=session_id, mention=mention)
    return await post_policy_query(
        parameters=parameters,
        session_id=session_id,
        clinical_context=clinical_context,
    )

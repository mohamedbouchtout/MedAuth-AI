"""The seam between a detected procedure and ``POST /policies/query``.

The transcript consumer knows a procedure was mentioned, in which session, and
what was said around it. ``/policies/query`` needs rather more than that:
``procedure``, ``cpt_code``, ``payer``, ``plan_type``, ``state``,
``clinical_context``, ``session_id`` and ``provider_id``. This module is where
the first list becomes the second, and it is deliberately split so that the part
which cannot be built yet is one named function rather than a hole spread
through the consumer.

**What resolves, and what still has no source.** TASK-024 filled in most of
this. ``cpt_code`` comes from :mod:`track_b_rag.procedure_codes`, which maps a
spoken keyword and its qualifier onto a code and refuses rather than guesses.
``provider_id`` is read from the encounter, where it has always been non-null.
``payer``, ``plan_type`` and ``state`` are columns on ``encounters`` that
nothing populates yet: they are filled from a FHIR ``Coverage`` resource at
SMART launch, which is **TASK-052b**, gated on TASK-051 and TASK-052. So
:func:`resolve_query_parameters` still raises on every real encounter — but it
now raises naming the three fields that are genuinely absent for *that*
encounter, rather than a fixed list of five.

A placeholder for any of them would be worse than no answer at all. The Redis
cache key is ``rag:{payer}:{plan_type}:{state}:{cpt_code}``, so a made-up value
writes a real policy answer under a key that stands for a different procedure or
a different plan, and unrelated encounters then collide on it. That is a wrong
answer served confidently to the next patient, which is strictly worse than the
silence of not querying.

**Reading the encounter is not a PHI access, and the query says so.** The SELECT
names ``provider_id``, ``insurance_payer``, ``insurance_plan_type`` and
``state`` and nothing else — no ``patient_fhir_id``, no ``insurance_member_id``,
no ``ehr_encounter_id``. Those four describe who is asking and which payer
policy applies, not the patient, so no ``audit_log()`` row is written here and
the audit obligation stays where it already is: one row per ``/policies/query``
call, written by the route. Known Constraints #6 asks for this to be decided
rather than guessed, and naming the columns explicitly is what makes the
decision hold — a later ``select(Encounter)`` would quietly turn this into a
PHI read, and it would not be quiet in review.

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
import sqlalchemy as sa

from track_a_clinical.models import Encounter
from track_b_rag.api.schemas import PolicyQueryData
from track_b_rag.config import get_settings
from track_b_rag.db import get_sessionmaker
from track_b_rag.keywords import ProcedureMention
from track_b_rag.procedure_codes import ProcedureCode, resolve_procedure_code

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
        reason: Why, in a sentence, when there is more to say than a field name.
            A missing ``cpt_code`` has four quite different causes and only one
            of them is worth acting on, so the field name alone would hide the
            distinction :mod:`track_b_rag.procedure_codes` exists to draw. Fixed
            text from this repo, never anything derived from the transcript.
    """

    def __init__(self, fields: tuple[str, ...], reason: str | None = None) -> None:
        self.fields = fields
        self.reason = reason
        message = f"No source yet for: {', '.join(fields)}"
        super().__init__(f"{message} — {reason}" if reason else message)


#: What :func:`resolve_query_parameters` still cannot supply for any encounter,
#: because nothing populates these three columns until **TASK-052b** fills them
#: from a FHIR ``Coverage`` resource at SMART launch. Named as data rather than
#: buried in the raise so the consumer's tests, and anyone reading the warning
#: in a log, can see the list without reading the function.
#:
#: The raise itself reports the subset actually missing for the encounter at
#: hand, which is a smaller list once TASK-052b starts filling some of them in.
UNRESOLVED_PARAMETERS: Final[tuple[str, ...]] = (
    "payer",
    "plan_type",
    "state",
)


def procedure_key(mention: ProcedureMention) -> str:
    """Return what counts as "the same procedure" for one mention.

    This is the dedup identity :func:`track_b_rag.dedup.claim_procedure` claims,
    and it is the CPT code wherever one resolves, so that two keywords naming one
    code share a claim and raise one nudge between them. Before TASK-024 the
    caller passed the canonical keyword and they did not.

    Args:
        mention: The detected procedure.

    Returns:
        ``"cpt:73721"`` when the code resolves, and ``"keyword:MRI"`` when it
        does not. Prefixed so that the set's contents say which kind of claim
        each member is when someone reads it out of Redis, and so a keyword can
        never collide with a code.

    Resolving here and again inside :func:`resolve_query_parameters` is
    deliberate: the lookup is pure and runs over one sentence, and the
    alternative is threading a resolved code through the consumer's dispatch
    signature so that the guard and the query cannot disagree. They cannot
    disagree anyway — same function, same input.
    """
    outcome = resolve_procedure_code(mention)
    if isinstance(outcome, ProcedureCode):
        return f"cpt:{outcome.cpt_code}"
    return f"keyword:{mention.keyword}"


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
        MissingQueryParameters: When the procedure has no confident code, when
            the encounter is unknown, or when the encounter's payer columns are
            still empty — which is every real encounter until TASK-052b. All
            three are structural: the same mention a second later fails
            identically, so the consumer keeps its dedup claim and logs once.
        Exception: Anything the database raises propagates, and is deliberately
            *not* turned into ``MissingQueryParameters``. A connection that
            failed once may work on the next mention, and the consumer releases
            the claim for exactly that case.
    """
    outcome = resolve_procedure_code(mention)
    if not isinstance(outcome, ProcedureCode):
        raise MissingQueryParameters(("cpt_code",), reason=f"{outcome.reason}: {outcome.detail}")

    # Only the four non-patient columns — provider, payer, plan type, state.
    # This is what makes reading the encounter a non-PHI access rather than an
    # unaudited one, so there is deliberately no audit_log() call here.
    #
    # That omission is correct only because the audit happens one level up: the
    # /policies/query route (TASK-012) writes one audit row per call, and this
    # dispatcher reaches it over HTTP precisely so that row cannot be bypassed
    # (see the module docstring). If you are reading this because an unaudited
    # PHI-adjacent SELECT looked like a bug — it is not, but the reasoning is
    # load-bearing in both directions. Adding a patient column here (
    # patient_fhir_id, insurance_member_id, ehr_encounter_id) turns this into a
    # real PHI read that TASK-012's row does not cover, and the audit obligation
    # would then genuinely be missing. Change the columns and you change the
    # compliance answer.
    statement = sa.select(
        Encounter.provider_id,
        Encounter.insurance_payer,
        Encounter.insurance_plan_type,
        Encounter.state,
    ).where(
        Encounter.session_id == session_id,
        Encounter.deleted_at.is_(None),
    )
    async with get_sessionmaker()() as session:
        row = (await session.execute(statement)).one_or_none()

    if row is None:
        # Structural rather than transient. TASK-006 creates the row and only
        # then publishes to sessions:started, so a subscriber cannot see a
        # segment for a session whose row has yet to be written; absent here
        # means soft-deleted or never real, and neither improves by waiting.
        raise MissingQueryParameters(
            UNRESOLVED_PARAMETERS,
            reason="no active encounter for this session",
        )

    missing = tuple(
        name
        for name, value in (
            ("payer", row.insurance_payer),
            ("plan_type", row.insurance_plan_type),
            ("state", row.state),
        )
        if not value
    )
    if missing:
        raise MissingQueryParameters(
            missing, reason="not populated until a SMART launch supplies it"
        )

    return PolicyQueryParameters(
        procedure=outcome.procedure,
        cpt_code=outcome.cpt_code,
        # The payer's own spelling, deliberately. `/policies/query` resolves it
        # through payer_vocab.normalize_payer and warns on an unknown slug
        # (api/query.py), and that is the single normalisation site; doing it
        # here as well would put one rule in two places for no gain.
        payer=row.insurance_payer,
        plan_type=row.insurance_plan_type,
        state=row.state,
        provider_id=row.provider_id,
    )


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
        MissingQueryParameters: When the query cannot be built at all — an
            unmappable procedure, an unknown encounter, or the payer columns
            TASK-052b fills, which is still every real encounter. Deliberately
            not swallowed here: the consumer distinguishes "this can never work"
            from "this did not work this time" and handles the dedup claim
            differently for each.
    """
    parameters = await resolve_query_parameters(session_id=session_id, mention=mention)
    return await post_policy_query(
        parameters=parameters,
        session_id=session_id,
        clinical_context=clinical_context,
    )

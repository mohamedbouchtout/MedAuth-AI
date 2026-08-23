"""The Da Vinci CRD tier — the payer's own answer to "is authorization required".

CMS-0057-F requires Medicare Advantage, Medicaid managed care, CHIP and ACA
marketplace payers to expose a standardized CDS Hooks API for coverage
requirements by 2027-01-01. Where a payer offers one, its answer to *does this
need prior authorization* is authoritative in a way that reasoning over its
published PDFs is not, so this module asks it and Stage 1 uses the answer.

**What CRD does and does not carry, established against real output.** The
CRD Reference Implementation was run locally and queried for several codes; the
IG's own ``ext-coverage-information`` StructureDefinition was then read to check
whether what came back was an RI shortcut or the shape of the standard. It is
the shape of the standard: the extension's slices are ``covered``,
``pa-needed``, ``doc-needed``, ``doc-purpose``, ``info-needed``,
``questionnaire``, ``reason``, ``detail``, ``billingCode`` and assorted dates
and trace identifiers. There is no slice carrying the criteria themselves.
CRD answers *whether* authorization and documentation are needed and points at
a DTR Questionnaire for *what* must be documented.

That is why this module maps to ``requires_auth`` alone and the RAG path still
supplies ``auth_criteria`` and the step therapy fields. A CRD-only answer would
hand :mod:`track_b_rag.gap_analysis` an empty criteria list, and a nudge that
cannot say what is missing is most of the product. The DTR half — following the
questionnaire canonical and turning its items into criteria — is deferred: its
items are largely administrative form fields (name, NPI, signature), so mapping
them into ``auth_criteria`` would have the matcher report a clinician's note as
missing "Signature".

**Two dialects, both real, and this reads both.** A spec-conformant payer states
the determination in the ``pa-needed`` slice. The Reference Implementation never
emits that slice — it emits a ``coverageInfo`` slice that is not in the IG's
slice list at all, and states the determination in the card's *type*
(``source.topic.code == "prior-auth"``). A mapping written against the IG alone
finds nothing in RI output; one written against RI output alone misses a real
payer. So :func:`read_determination` checks the conformant signal first and
falls back to the card type.

**No patient reaches this module**, the same as the rest of Stage 1. The request
below is built from payer, plan type, state and procedure code only, with a
placeholder subject carrying no demographics. That is a real constraint and not
a simplification: CRD is specified as a patient-specific coverage check, and a
payer rule that needs an age or a sex cannot be evaluated from what Stage 1 is
allowed to know. Those requests come back as an "unable to process" card or an
error, which this module reports as *no determination* — and the RAG path then
answers, as it did before. Sending a fabricated patient to make such a rule
answer would produce a confident determination about someone who does not
exist. Patient-specific CRD belongs with the EHR-backed FHIR resources
fhir-integration will have in Phase 5.

**Nothing here is cached.** The entire value of this tier over the RAG path is
that it is live and authoritative at the moment of the order; storing it for a
day would leave a slower, more complex way to get a stale answer. See CLAUDE.md,
"A CRD answer is never cached; a RAG answer is."
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

#: Payer slugs whose plans CMS-0057-F covers, as canonical slugs from
#: ``packages/payer-vocab`` — never display names, for the reason the whole
#: vocabulary exists. Deliberately a literal set and not a configuration file:
#: it is two entries today and becomes real payer capability data once payers
#: publish endpoints, which is a later task and not something to build a
#: framework for now.
#:
#: The mandate also covers CHIP and ACA marketplace plans. Neither has a slug
#: yet, because no `Coverage.payor.display` observed so far has produced one —
#: adding speculative slugs here would be the "extend the alias table from
#: plausible spellings" mistake the vocabulary's own notes rule out. Add each
#: when a real payer name resolves to it.
CRD_SUPPORTED_PAYERS: Final[frozenset[str]] = frozenset(
    {
        "medicare-advantage",
        "medicaid",
    }
)

#: The CRD services this asks. ``order-sign`` is the hook fired as an order is
#: signed, which is the closest thing to what a nudge is reacting to.
ORDER_SIGN_SERVICE: Final = "order-sign-crd"

#: Path prefix on the CRD server. The CRD RI's README documents its discovery
#: endpoint as ``/cds-services``; the running server answers 404 there and 200
#: on ``/r4/cds-services``, because the controller prefixes every route with the
#: FHIR release. Taken from the running server, not the README.
CDS_SERVICES_PATH: Final = "/r4/cds-services"

#: HCPCS Level II codes are a letter followed by four digits; CPT codes are five
#: digits. The two live in different code systems and a payer matches on the
#: system as well as the code, so the request has to name the right one.
_HCPCS_PATTERN: Final = re.compile(r"^[A-Z]\d{4}$")

CPT_SYSTEM: Final = "http://www.ama-assn.org/go/cpt"
HCPCS_SYSTEM: Final = "https://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets"

COVERAGE_INFORMATION_URL: Final = (
    "http://hl7.org/fhir/us/davinci-crd/StructureDefinition/ext-coverage-information"
)

#: ``pa-needed`` values that mean authorization is required. ``conditional``
#: counts: the payer is saying it may be required, and Stage 1 reporting "not
#: required" on a maybe is the failure direction TASK-012 rules out.
_AUTH_REQUIRED_CODES: Final[frozenset[str]] = frozenset({"auth-needed", "performpa", "conditional"})

#: ``pa-needed`` values that mean it is not. ``satisfied`` means an
#: authorization already exists for this order, so nothing is outstanding.
_AUTH_NOT_REQUIRED_CODES: Final[frozenset[str]] = frozenset({"no-auth", "satisfied"})

#: The card type the Reference Implementation uses to say the same thing. Note
#: there is no negative counterpart: a payer with nothing to require simply
#: sends no prior-auth card, and silence is not a "no" — see
#: :func:`read_determination`.
_PRIOR_AUTH_CARD_TYPE: Final = "prior-auth"

#: The placeholder the request's subject and coverage are hung off. It is not a
#: patient identifier and is not derived from one; it exists because CDS Hooks
#: requires the context to name a subject.
_PLACEHOLDER_ID: Final = "policy-query"


@dataclass(frozen=True)
class CrdDetermination:
    """The payer's answer about prior authorization, and how it said so.

    ``signal`` names which of the two dialects produced the answer — a
    ``pa-needed`` slice or a ``prior-auth`` card. It is logged rather than
    returned to callers, so that a determination that looks wrong can be traced
    to what the payer actually sent without re-running the request.
    """

    requires_auth: bool
    signal: str


def is_crd_supported(payer: str) -> bool:
    """Return whether this payer is expected to answer over Da Vinci CRD.

    Args:
        payer: The canonical payer slug from ``packages/payer-vocab``. A display
            name will not match, which is the same failure the vocabulary exists
            to prevent — callers normalise before Stage 1, not here.

    Returns:
        True for a payer covered by the CMS-0057-F mandate.
    """
    return payer in CRD_SUPPORTED_PAYERS


def code_system(cpt_code: str) -> str:
    """Return the code system URI a procedure code belongs to."""
    return HCPCS_SYSTEM if _HCPCS_PATTERN.match(cpt_code) else CPT_SYSTEM


def build_hook_request(
    *,
    procedure: str,
    cpt_code: str,
    plan_type: str,
    state: str,
) -> dict[str, Any]:
    """Return the CDS Hooks ``order-sign`` request body for one policy question.

    The order is a draft ``ServiceRequest`` carrying the procedure code, and the
    prefetch bundle carries it alongside a placeholder subject and a Coverage
    naming the plan type and state. No demographics are set, because Stage 1
    holds none — see this module's docstring for what that costs and why the
    alternative is worse.

    Args:
        procedure: The procedure as the clinician described it. Sent as the
            code's display text, where it serves as a human-readable label only.
        cpt_code: The authoritative procedure code.
        plan_type: Plan type, e.g. ``PPO``.
        state: Two-letter state code.

    Returns:
        A request body ready to POST to the payer's ``order-sign`` service.
    """
    service_request = {
        "resourceType": "ServiceRequest",
        "id": _PLACEHOLDER_ID,
        "status": "draft",
        "intent": "order",
        "code": {
            "coding": [{"system": code_system(cpt_code), "code": cpt_code}],
            "text": procedure,
        },
        "subject": {"reference": f"Patient/{_PLACEHOLDER_ID}"},
        "insurance": [{"reference": f"Coverage/{_PLACEHOLDER_ID}"}],
    }
    coverage = {
        "resourceType": "Coverage",
        "id": _PLACEHOLDER_ID,
        "status": "active",
        "beneficiary": {"reference": f"Patient/{_PLACEHOLDER_ID}"},
        "payor": [{"reference": f"Organization/{_PLACEHOLDER_ID}"}],
        "class": [{"value": plan_type}],
        # The jurisdiction the question is being asked about. A payer that
        # varies its rules by state reads it here; the RI ignores it.
        "extension": [
            {
                "url": "http://hl7.org/fhir/StructureDefinition/coverage-jurisdiction",
                "valueString": state,
            }
        ],
    }
    patient = {"resourceType": "Patient", "id": _PLACEHOLDER_ID}
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": resource} for resource in (patient, coverage, service_request)],
    }
    return {
        "hook": "order-sign",
        # Per-request identifier, required by CDS Hooks. Random, and carries
        # nothing about the session it was asked for — this request goes to a
        # third party.
        "hookInstance": str(uuid.uuid4()),
        "context": {
            "userId": f"PractitionerRole/{_PLACEHOLDER_ID}",
            "patientId": _PLACEHOLDER_ID,
            "draftOrders": {
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [{"resource": service_request}],
            },
        },
        "prefetch": {"serviceRequestBundle": bundle},
    }


def read_determination(response: Mapping[str, Any]) -> CrdDetermination | None:
    """Return the prior authorization determination in a CDS Hooks response.

    Reads the conformant signal first — a ``pa-needed`` value inside an
    ``ext-coverage-information`` extension — and falls back to the card type the
    Reference Implementation uses instead. See this module's docstring for why
    both are necessary.

    Returns:
        The determination, or None when the response decides nothing. None is
        the answer for an empty card list, for cards that only report
        documentation requirements, and for the "unable to process" card a payer
        returns when its rule needs more than was sent. **Silence is never read
        as "no authorization required"**: a payer that said nothing has not told
        us the order is clear, and treating it as such is the one direction
        TASK-012 forbids failing in. Those all fall through to the RAG path.
    """
    cards = response.get("cards")
    if not isinstance(cards, Sequence) or isinstance(cards, str | bytes):
        return None

    prior_auth_card = False
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        for value in _pa_needed_values(card):
            if value in _AUTH_REQUIRED_CODES:
                return CrdDetermination(requires_auth=True, signal=f"pa-needed:{value}")
            if value in _AUTH_NOT_REQUIRED_CODES:
                return CrdDetermination(requires_auth=False, signal=f"pa-needed:{value}")
        if _card_type(card) == _PRIOR_AUTH_CARD_TYPE:
            prior_auth_card = True

    if prior_auth_card:
        return CrdDetermination(requires_auth=True, signal="card-type:prior-auth")
    return None


async def determine(
    *,
    base_url: str,
    timeout_seconds: float,
    procedure: str,
    cpt_code: str,
    payer: str,
    plan_type: str,
    state: str,
) -> CrdDetermination | None:
    """Ask the payer whether this procedure needs prior authorization.

    Args:
        base_url: The payer's CRD server root.
        timeout_seconds: How long to wait before giving up and letting the RAG
            path answer alone.
        procedure: The procedure as the clinician described it.
        cpt_code: The authoritative procedure code.
        payer: Canonical payer slug, used for logging only — the endpoint is
            already the payer's.
        plan_type: Plan type, e.g. ``PPO``.
        state: Two-letter state code.

    Returns:
        The determination, or None if the payer did not make one. Every failure
        mode returns None rather than raising: a timeout, a transport error, a
        non-2xx status, a body that is not JSON, and a response that decides
        nothing all mean the same thing to the caller, which is that the RAG
        path answers by itself. This tier can only ever add an answer; it can
        never take the existing one away.
    """
    context = f"{payer}/{plan_type}/{state} CPT {cpt_code}"
    url = f"{base_url.rstrip('/')}{CDS_SERVICES_PATH}/{ORDER_SIGN_SERVICE}"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                url,
                json=build_hook_request(
                    procedure=procedure,
                    cpt_code=cpt_code,
                    plan_type=plan_type,
                    state=state,
                ),
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        # Includes httpx transport and status errors and a body that will not
        # parse. Logged at WARNING rather than ERROR: the query still gets an
        # answer, so this is a degraded tier and not a failed request.
        logger.warning("CRD lookup failed for %s; falling through to RAG", context, exc_info=True)
        return None

    if not isinstance(payload, Mapping):
        logger.warning("CRD returned a non-object response for %s", context)
        return None

    determination = read_determination(payload)
    if determination is None:
        logger.info("CRD made no authorization determination for %s", context)
        return None

    logger.info(
        "CRD determined requires_auth=%s for %s via %s",
        determination.requires_auth,
        context,
        determination.signal,
    )
    return determination


def _pa_needed_values(card: Mapping[str, Any]) -> list[str]:
    """Return every ``pa-needed`` code in a card's coverage-information extensions.

    The extension rides on the resource inside a suggestion's action, which is
    where CRD puts the annotated copy of the submitted order.
    """
    values: list[str] = []
    for extension in _coverage_information(card):
        for part in _as_mappings(extension.get("extension")):
            if part.get("url") != "pa-needed":
                continue
            value = part.get("valueCode")
            if value is None:
                value = (part.get("valueCoding") or {}).get("code")
            if isinstance(value, str):
                values.append(value)
    return values


def _coverage_information(card: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the coverage-information extensions attached to a card."""
    found: list[Mapping[str, Any]] = []
    for suggestion in _as_mappings(card.get("suggestions")):
        for action in _as_mappings(suggestion.get("actions")):
            resource = action.get("resource")
            if not isinstance(resource, Mapping):
                continue
            for extension in _as_mappings(resource.get("extension")):
                if extension.get("url") == COVERAGE_INFORMATION_URL:
                    found.append(extension)
    return found


def _card_type(card: Mapping[str, Any]) -> str | None:
    """Return a card's Da Vinci card type, or None if it does not declare one."""
    source = card.get("source")
    if not isinstance(source, Mapping):
        return None
    topic = source.get("topic")
    if not isinstance(topic, Mapping):
        return None
    code = topic.get("code")
    return code if isinstance(code, str) else None


def _as_mappings(value: Any) -> list[Mapping[str, Any]]:
    """Return `value` as a list of mappings, discarding anything else.

    A payer's response is third-party JSON: every level of the walk above has to
    tolerate a missing key, a null, or a value of the wrong type without raising.
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, Mapping)]

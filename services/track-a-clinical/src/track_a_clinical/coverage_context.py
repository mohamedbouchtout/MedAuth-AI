"""Reading an encounter's payer and place from the EHR, at session start.

TASK-052b. ``POST /sessions/start`` calls this when the client hands it a
``launch_id`` and an ``ehr_encounter_id``, and writes what comes back onto three
``encounters`` columns: ``insurance_payer``, ``insurance_plan_type`` and
``state``. Those are the three parameters ``resolve_query_parameters()`` has
never been able to fill, so until this runs no policy query can be built for any
real encounter and no nudge can fire.

**It goes over HTTP even though both services share a virtualenv.** The
``audit_log()`` row for a chart read is written by ``fhir-integration``'s route
layer, and importing its adapter here would skip that row while still reading a
patient's coverage. Moving the audit down into the adapter so both paths were
covered would put one compliance obligation in two places, and two
hand-maintained copies of an obligation drift. Same argument, and the same
arrangement, as ``track_b_rag.policy_dispatch`` posting to ``/policies/query``.

**A failure here does not fail the session.** The columns are nullable and the
dispatcher already names exactly which of them are missing, per procedure. An
encounter that cannot start because a payer lookup timed out is a provider
unable to record a visit, which is worse than a visit whose policy queries
cannot be built — and the second failure is visible in the dispatcher's log
either way. The trade is deliberate rather than incidental: it is the reason
this returns ``None`` on every failure instead of raising.

**Nothing here is written from a guess.** A field the EHR did not hold stays
NULL. The cache key is ``rag:{payer}:{plan_type}:{state}:{cpt_code}``, so a
fabricated segment writes a real policy answer under a key standing for a
different plan and serves it to the next encounter that matches — silently, and
across patients.
"""

from __future__ import annotations

import logging
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from track_a_clinical.config import get_settings

logger = logging.getLogger(__name__)

#: The header ``fhir-integration`` reads ``launch_id`` from. Not a query
#: parameter and not a path segment: a ``launch_id`` resolves to an EHR access
#: token, which makes it a capability handle, and this repository keeps that
#: class of value out of URLs that intermediaries log.
LAUNCH_ID_HEADER: Final = "X-MedAuth-Launch-Id"


def coverage_context_path(ehr_encounter_id: str) -> str:
    """Return the path of the route that answers for one EHR encounter."""
    return f"/fhir/encounter/{ehr_encounter_id}/coverage-context"


class CoverageInfo(BaseModel):
    """The payer half of the answer, as ``fhir-integration`` returns it.

    A local mirror of that service's response model rather than an import: the
    two services are separate deployables talking over HTTP, and importing the
    shape would couple this service's build to the other's package. Only the
    fields written onto a column are declared, and unknown fields are ignored,
    so a field added there does not break a client here.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    payer: str | None = None
    plan_type: str | None = None
    member_id: str | None = None


class EncounterCoverageContext(BaseModel):
    """What one encounter's payer columns should be set to.

    Attributes:
        coverage: The payer half, or None when the EHR held nothing usable.
        state: The **site-of-care** state as a two-character USPS code, already
            normalised by ``fhir-integration``. Never the patient's residence —
            the policy documents scope themselves by where the service took
            place. See that service's ``site_of_care`` module.
        requires_manual_confirmation: True when the payer information is
            incomplete and a provider has to supply it.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    coverage: CoverageInfo | None = None
    state: str | None = None
    requires_manual_confirmation: bool = False


async def fetch_coverage_context(
    *,
    launch_id: str,
    ehr_encounter_id: str,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
) -> EncounterCoverageContext | None:
    """Ask ``fhir-integration`` what this encounter's payer and place are.

    Args:
        launch_id: The SMART launch whose EHR access token the read is made
            with. A credential handle — never logged, and never put in the URL.
        ehr_encounter_id: The encounter's id on the EHR.
        base_url: Override for ``fhir-integration``'s base URL.
        timeout_seconds: Override for the request timeout.

    Returns:
        The answer, or None when the call could not be completed — a timeout, a
        transport error, a non-2xx, or a body that does not match the model. The
        caller leaves all three columns NULL in that case; see the module
        docstring for why that is not an error.

    Note the log lines name the encounter and nothing else. The ``launch_id`` is
    a credential and the response carries a payer and a member id, which are
    PHI-adjacent and PHI respectively.
    """
    settings = get_settings()
    url = (base_url or settings.fhir_integration_url).rstrip("/")
    url += coverage_context_path(ehr_encounter_id)

    try:
        timeout = timeout_seconds or settings.fhir_integration_timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers={LAUNCH_ID_HEADER: launch_id})
            response.raise_for_status()
            payload = response.json()
        return EncounterCoverageContext.model_validate(payload["data"])
    except (httpx.HTTPError, ValidationError, KeyError, TypeError, ValueError):
        # WARNING rather than ERROR: the session still starts and the encounter
        # is usable for everything except a policy query, and the dispatcher
        # reports the missing parameters again per procedure. `exc_info` is safe
        # — these exceptions carry a URL and a status, never a response body.
        logger.warning(
            "Could not read the payer context for EHR encounter %s. The encounter's "
            "payer, plan type and state stay NULL, so policy queries for this visit "
            "cannot be built until they are supplied.",
            ehr_encounter_id,
            exc_info=True,
        )
        return None

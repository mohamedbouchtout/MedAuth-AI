"""Reading a patient's chart context at bundle assembly time.

``GET /fhir/patient/{patient_id}/context`` in ``fhir-integration`` (TASK-052).

**It goes over HTTP even though both services share a virtualenv.** The
``READ_PATIENT`` audit row for a chart read is written by that service's route
layer, and importing its adapter here would skip the row while still reading a
patient's coverage. Moving the audit down into the adapter so both paths were
covered would put one compliance obligation in two places, and two
hand-maintained copies of an obligation drift. Same argument, and the same
arrangement, as track-a-clinical's coverage-context read at session start.

**What the call is for, stated plainly, because the answer is narrower than it
looks.** ``prior_auth_requests`` has no patient or provider column — those are
reached through the encounter — so the only field this read contributes is
``payer_name``, when the encounter's own ``insurance_payer`` is NULL because the
coverage lookup at session start did not succeed. Its other purpose is the one
TASK-060 actually needs: it establishes that the EHR still resolves this patient
and their coverage under this launch. Assembling a bundle without that produces
a row that looks complete on TASK-072's dashboard and is unsubmittable at the
payer, which is the failure mode the task exists to avoid.

**A failure here is not a partial bundle.** The caller writes nothing, which
leaves the assembly retryable — nothing was recorded, so there is nothing to
reconcile. That is the opposite trade from the session-start read, and
deliberately so: there, failing would stop a provider recording a visit, and the
columns it fills are nullable and reported as missing further down. Here there
is no provider waiting and no later reporter of the gap.

The ``launch_id`` is a credential handle and never appears in a log line, an
exception message or a URL.
"""

from __future__ import annotations

import logging
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from prior_auth.config import get_settings

logger = logging.getLogger(__name__)

#: The header ``fhir-integration`` reads ``launch_id`` from. Not a query
#: parameter and not a path segment: a ``launch_id`` resolves to an EHR access
#: token, which makes it a capability handle, and this repository keeps that
#: class of value out of URLs that intermediaries log.
LAUNCH_ID_HEADER: Final = "X-MedAuth-Launch-Id"


def patient_context_path(patient_id: str) -> str:
    """Return the path of the route that answers for one patient."""
    return f"/fhir/patient/{patient_id}/context"


class CoverageInfo(BaseModel):
    """The payer half of the answer, as ``fhir-integration`` returns it.

    A local mirror of that service's response model rather than an import: the
    two services are separate deployables talking over HTTP, and importing the
    shape would couple this service's build to the other's package. Only the
    fields read here are declared, and unknown fields are ignored, so a field
    added there does not break a client here.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    payer: str | None = None
    plan_type: str | None = None
    member_id: str | None = None


class PatientContext(BaseModel):
    """One patient's chart context, as far as this service reads it.

    Attributes:
        coverage: The payer half, or None when the EHR held nothing usable.
        requires_manual_confirmation: True when the payer information is
            incomplete and a provider has to supply it. Carried so the caller
            can log the distinction; it is not written to any column here.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    coverage: CoverageInfo | None = None
    requires_manual_confirmation: bool = False


async def fetch_patient_context(
    *,
    launch_id: str,
    patient_id: str,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
) -> PatientContext | None:
    """Read one patient's demographics, coverage and conditions from the EHR.

    Args:
        launch_id: The SMART launch whose EHR access token the read is made
            with. A credential handle — never logged, and never put in the URL.
        patient_id: The patient as the EHR knows them.
        base_url: Override for ``fhir-integration``'s base URL.
        timeout_seconds: Override for the request timeout.

    Returns:
        The context, or None when the call could not be completed — a timeout, a
        transport error, a non-2xx, or a body that does not match the model. The
        caller assembles no bundle in that case; see the module docstring.

    The log line names the patient and nothing else. The response carries a
    payer and a member id, which are PHI-adjacent and PHI respectively.
    """
    settings = get_settings()
    url = (base_url or settings.fhir_integration_url).rstrip("/")
    url += patient_context_path(patient_id)

    try:
        timeout = timeout_seconds or settings.fhir_integration_timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers={LAUNCH_ID_HEADER: launch_id})
            response.raise_for_status()
            payload = response.json()
        return PatientContext.model_validate(payload["data"])
    except (httpx.HTTPError, ValidationError, KeyError, TypeError, ValueError):
        # ERROR rather than WARNING: unlike the session-start read, nothing
        # downstream reports this gap again and no bundle is written, so this
        # line is the only record that an encounter with flagged procedures
        # produced no prior-authorization request. `exc_info` is safe — these
        # exceptions carry a URL and a status, never a response body.
        logger.error(
            "Could not read the chart context for patient %s; no prior-authorization "
            "bundle will be assembled for this encounter",
            patient_id,
            exc_info=True,
        )
        return None

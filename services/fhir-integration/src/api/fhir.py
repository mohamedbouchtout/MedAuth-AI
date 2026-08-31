"""``GET /fhir/patient/{id}/context`` and ``GET /fhir/encounter/{id}`` — reading the EHR.

TASK-052. These are the first routes in this repository that read a patient's
chart, and the first PHI accesses in this service.

**Both are keyed on ``launch_id``, carried in the ``X-MedAuth-Launch-Id``
header.** Not on ``session_id``: a SMART launch and an encounter session are two
different things with two different lifetimes, neither derivable from the other,
and at the time these routes run an encounter may not exist at all. Settled in
CLAUDE.md, "A SMART launch is not an encounter session". A ``session_id``
presented in that header finds no ``fhir_token:`` record and gets a 404 like any
other unknown launch — there is deliberately no fallback that tries it as the
other kind of identifier.

**Why a header rather than a query parameter or a path segment.** ``launch_id``
resolves to an EHR access token, so holding one is enough to read a chart. That
makes it a capability handle, and this repository already refuses to put that
class of value in a URL query string — "the third thing browsers can carry and
the one place a credential is certain to be logged by intermediaries". A path
segment lands in the same access logs. A browser ``fetch()`` sets request
headers freely, unlike the native ``WebSocket`` constructor that forced the
subprotocol carrier elsewhere, so nothing is given up by choosing a header.

**The vendor is never a request parameter.** ``ehr_type`` comes back out of the
launch record, and the handler asks ``get_adapter()`` for whatever answers.
Neither route imports a concrete adapter, and neither branches on which EHR it
is talking to.
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

import httpx
from fastapi import APIRouter, Depends, Header, Path, status
from redis.asyncio import Redis

from api_envelope import ApiHTTPException, ApiResponse, error_responses
from fhir_types import Encounter
from hipaa_logger import AuditAction
from src.adapters import get_adapter
from src.adapters.base import EHRAdapter
from src.adapters.errors import (
    FHIRAuthorizationExpired,
    FHIRMalformedResponse,
    FHIRResourceNotFound,
    FHIRUpstreamUnavailable,
)
from src.adapters.models import EncounterCoverageContext, PatientContext
from src.api.dependencies import get_http_client, get_redis
from src.audit import (
    RESOURCE_TYPE_ENCOUNTER,
    RESOURCE_TYPE_PATIENT,
    audit_ehr_read,
)
from src.smart import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fhir", tags=["fhir"])

#: The header carrying ``launch_id``. Named here so the route, the CORS
#: allow-list in ``packages/cors-policy`` and the tests cannot spell it three
#: ways.
LAUNCH_ID_HEADER: Final = "X-MedAuth-Launch-Id"

ERROR_CODE_UNKNOWN_LAUNCH: Final = "FHIR_UNKNOWN_LAUNCH"
ERROR_CODE_LAUNCH_EXPIRED: Final = "FHIR_LAUNCH_EXPIRED"
ERROR_CODE_NOT_FOUND: Final = "FHIR_RESOURCE_NOT_FOUND"
ERROR_CODE_UPSTREAM_UNAVAILABLE: Final = "FHIR_UPSTREAM_UNAVAILABLE"
ERROR_CODE_MALFORMED: Final = "FHIR_MALFORMED_RESPONSE"


async def get_ehr_adapter(
    x_medauth_launch_id: Annotated[
        str,
        Header(
            alias=LAUNCH_ID_HEADER,
            description=(
                "The launch_id returned by GET /fhir/callback. Names the SMART "
                "launch whose EHR access token this read is made with. Not a "
                "session_id — the two are different identifiers with different "
                "lifetimes."
            ),
        ),
    ],
    redis: Annotated[Redis, Depends(get_redis)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> EHRAdapter:
    """Resolve the launch's stored token into an adapter for that EHR.

    A missing header is a 422 from the standard validation handler, like any
    other absent required parameter. An unknown one — expired, never issued, or
    a ``session_id`` sent by mistake — is a **404**, not a 401: nothing is being
    rejected, there is simply no such launch. The presented value is never
    logged.

    Raises:
        ApiHTTPException: 404 when no launch record answers.
    """
    token = await store.load_launch_token(redis, x_medauth_launch_id)
    if token is None:
        logger.info("No launch record for the presented launch id — answering 404.")
        raise ApiHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ERROR_CODE_UNKNOWN_LAUNCH,
            message=(
                "No such SMART launch. It may have expired, in which case the "
                "launch must be repeated from the EHR."
            ),
        )

    return get_adapter(
        token.ehr_type,
        token.fhir_base_url,
        token.access_token,
        http_client,
    )


def _as_api_error(exc: Exception) -> ApiHTTPException:
    """Map one FHIR-layer failure onto its own envelope outcome.

    The three outcomes are distinct on purpose and must not be collapsed: only
    ``FHIRUpstreamUnavailable`` is worth retrying, and merging it with
    ``FHIRResourceNotFound`` would let an outage read as a patient with no
    insurance. See ``src/adapters/errors.py``.

    No response body, and in particular no ``OperationOutcome.diagnostics``,
    reaches the message — the exceptions never carry one.
    """
    if isinstance(exc, FHIRAuthorizationExpired):
        # The seam TASK-051b fills. Recognised here, in one place, rather than
        # inside each primitive.
        return ApiHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code=ERROR_CODE_LAUNCH_EXPIRED,
            message=(
                "The EHR rejected this launch's access token. The SMART launch "
                "must be repeated; automatic refresh arrives in TASK-051b."
            ),
        )
    if isinstance(exc, FHIRResourceNotFound):
        return ApiHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ERROR_CODE_NOT_FOUND,
            message=f"The EHR holds no {exc.resource_type} with that id.",
        )
    if isinstance(exc, FHIRUpstreamUnavailable):
        return ApiHTTPException(
            status_code=(
                status.HTTP_504_GATEWAY_TIMEOUT if exc.timed_out else status.HTTP_502_BAD_GATEWAY
            ),
            code=ERROR_CODE_UPSTREAM_UNAVAILABLE,
            message=(
                f"The EHR could not be reached for {exc.resource_type}. This is "
                "transient — retrying is reasonable."
            ),
        )
    if isinstance(exc, FHIRMalformedResponse):
        return ApiHTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code=ERROR_CODE_MALFORMED,
            message=(
                f"The EHR's {exc.resource_type} response was not usable. This is "
                "not transient; it needs an adapter for that vendor."
            ),
        )
    raise exc


@router.get(
    "/patient/{patient_id}/context",
    response_model=ApiResponse[PatientContext],
    status_code=status.HTTP_200_OK,
    summary="Read a patient's demographics, coverage and active conditions",
    response_description="The assembled patient context for one patient.",
    responses=error_responses(
        401,
        404,
        422,
        502,
        504,
        descriptions={
            401: "The EHR rejected this launch's access token; repeat the launch.",
            404: "No such SMART launch, or the EHR holds no such patient.",
            502: "The EHR was unreachable or answered with something unusable.",
            504: "The EHR did not answer in time.",
        },
    ),
)
async def read_patient_context(
    patient_id: Annotated[str, Path(description="The patient's id on the EHR.")],
    adapter: Annotated[EHRAdapter, Depends(get_ehr_adapter)],
) -> ApiResponse[PatientContext]:
    """Return one patient's demographics, insurance coverage and active conditions.

    Assembled by the adapter's composed ``get_patient_context()``, which is the
    method a vendor subclass overrides.

    **Incomplete payer information is not an error.** When the EHR holds no
    usable ``Coverage``, or one missing its payer or plan type,
    ``requires_manual_confirmation`` comes back true and the provider fills the
    gap. A guessed payer would write a real policy answer under a cache key
    standing for a plan the patient is not on, which is worse than an incomplete
    answer a human can see. The enumerated rule is in TASK-052.

    This is a PHI read and writes one ``READ_PATIENT`` audit row — one per call,
    not one per underlying FHIR fetch.
    """
    try:
        context = await adapter.get_patient_context(patient_id)
    except Exception as exc:
        raise _as_api_error(exc) from exc

    await audit_ehr_read(
        action=AuditAction.READ_PATIENT,
        resource_type=RESOURCE_TYPE_PATIENT,
        resource_id=patient_id,
    )
    return ApiResponse[PatientContext](data=context)


@router.get(
    "/encounter/{encounter_id}",
    response_model=ApiResponse[Encounter],
    status_code=status.HTTP_200_OK,
    summary="Read one encounter from the EHR",
    response_description="The FHIR R4 Encounter resource.",
    responses=error_responses(
        401,
        404,
        422,
        502,
        504,
        descriptions={
            401: "The EHR rejected this launch's access token; repeat the launch.",
            404: "No such SMART launch, or the EHR holds no such encounter.",
            502: "The EHR was unreachable or answered with something unusable.",
            504: "The EHR did not answer in time.",
        },
    ),
)
async def read_encounter(
    encounter_id: Annotated[str, Path(description="The encounter's id on the EHR.")],
    adapter: Annotated[EHRAdapter, Depends(get_ehr_adapter)],
) -> ApiResponse[Encounter]:
    """Return one ``Encounter`` resource as the EHR holds it.

    Unlike the patient context this is not normalized: an ``Encounter`` is
    returned as the R4 resource, because nothing above this layer needs a
    flattened form of it yet and inventing one now would fix a shape before
    there is a consumer to fix it for.

    This is a PHI read and writes one ``READ_ENCOUNTER`` audit row.
    """
    try:
        encounter = await adapter.get_encounter(encounter_id)
    except Exception as exc:
        raise _as_api_error(exc) from exc

    await audit_ehr_read(
        action=AuditAction.READ_ENCOUNTER,
        resource_type=RESOURCE_TYPE_ENCOUNTER,
        resource_id=encounter_id,
    )
    return ApiResponse[Encounter](data=encounter)


@router.get(
    "/encounter/{encounter_id}/coverage-context",
    response_model=ApiResponse[EncounterCoverageContext],
    status_code=status.HTTP_200_OK,
    summary="Read the payer and site-of-care details for one encounter",
    response_description="The payer half and the site-of-care state for one encounter.",
    responses=error_responses(
        401,
        404,
        422,
        502,
        504,
        descriptions={
            401: "The EHR rejected this launch's access token; repeat the launch.",
            404: "No such SMART launch, or the EHR holds no such encounter.",
            502: "The EHR was unreachable or answered with something unusable.",
            504: "The EHR did not answer in time.",
        },
    ),
)
async def read_encounter_coverage_context(
    encounter_id: Annotated[str, Path(description="The encounter's id on the EHR.")],
    adapter: Annotated[EHRAdapter, Depends(get_ehr_adapter)],
) -> ApiResponse[EncounterCoverageContext]:
    """Return the three things an ``encounters`` row needs about payer and place.

    TASK-052b. ``track-a-clinical`` calls this from ``POST /sessions/start`` to
    populate ``insurance_payer``, ``insurance_plan_type`` and ``state`` — the
    three parameters ``resolve_query_parameters()`` has never been able to fill.

    **Keyed on the encounter, and the patient is not a parameter.** The subject
    is read off ``Encounter.subject``, so a caller cannot pair one encounter
    with another patient's coverage.

    **``state`` is the site of care and never the patient's residence.** The
    payer documents this platform reads scope themselves that way; the reasoning
    and the evidence are in ``src/adapters/site_of_care.py``.

    **An incomplete answer is not an error.** A NULL column is the correct record
    of something the EHR did not hold, and the dispatcher downstream names
    exactly which fields are still missing. A guessed payer or a guessed state
    would instead write a real policy answer under a
    ``rag:{payer}:{plan_type}:{state}:{cpt_code}`` key standing for a different
    plan, and serve it to the next encounter that matched.

    This is a PHI read — it reads the patient's coverage — and writes one
    ``READ_PATIENT`` audit row, one per call rather than one per FHIR fetch.
    """
    try:
        context = await adapter.get_encounter_coverage_context(encounter_id)
    except Exception as exc:
        raise _as_api_error(exc) from exc

    # READ_PATIENT rather than READ_ENCOUNTER: what makes this a PHI access is
    # the Coverage read, and the encounter is how it was addressed. The resource
    # ids stay the encounter's, because that is what was asked for.
    await audit_ehr_read(
        action=AuditAction.READ_PATIENT,
        resource_type=RESOURCE_TYPE_ENCOUNTER,
        resource_id=encounter_id,
    )
    return ApiResponse[EncounterCoverageContext](data=context)

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
from src.api.dependencies import (
    get_app_settings,
    get_http_client,
    get_redis,
    require_credentials,
)
from src.audit import (
    RESOURCE_TYPE_ENCOUNTER,
    RESOURCE_TYPE_PATIENT,
    audit_ehr_read,
)
from src.config import Settings
from src.smart import store
from src.smart.oauth import (
    TokenEndpointUnavailable,
    TokenExchangeError,
    TokenGrantRejected,
    refresh_access_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fhir", tags=["fhir"])

#: The header carrying ``launch_id``. Named here so the route, the CORS
#: allow-list in ``packages/cors-policy`` and the tests cannot spell it three
#: ways.
LAUNCH_ID_HEADER: Final = "X-MedAuth-Launch-Id"

ERROR_CODE_UNKNOWN_LAUNCH: Final = "FHIR_UNKNOWN_LAUNCH"
ERROR_CODE_LAUNCH_EXPIRED: Final = "FHIR_LAUNCH_EXPIRED"
ERROR_CODE_REFRESH_UNAVAILABLE: Final = "FHIR_TOKEN_REFRESH_UNAVAILABLE"
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
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> EHRAdapter:
    """Resolve the launch's stored token into an adapter for that EHR.

    A missing header is a 422 from the standard validation handler, like any
    other absent required parameter. An unknown one — expired, never issued, or
    a ``session_id`` sent by mistake — is a **404**, not a 401: nothing is being
    rejected, there is simply no such launch. The presented value is never
    logged.

    **This is where an EHR access token is renewed** (TASK-051b), before an
    adapter exists and before any fetch is attempted, so no route body and no
    adapter primitive knows renewal happens at all. Reactive renewal — catching
    the EHR's 401 and retrying — was the alternative and is not possible here:
    this is a FastAPI dependency, so it has already returned by the time a route
    body's fetch raises, and a reactive path would mean a retry wrapper at every
    fetch call site and at every one added later. See CLAUDE.md, "The launch
    record outlives its access token", including what proactive renewal cannot
    catch.

    Raises:
        ApiHTTPException: 404 when no launch record answers; 401 when the launch
            is over and must be repeated; 502 or 504 when renewal could not be
            completed but the grant may still be good.
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

    if store.access_token_is_stale(token, skew_seconds=settings.smart_token_refresh_skew_seconds):
        token = await _renew_access_token(redis, http_client, settings, x_medauth_launch_id, token)

    return get_adapter(
        token.ehr_type,
        token.fhir_base_url,
        token.access_token,
        http_client,
    )


async def _renew_access_token(
    redis: Redis,
    http_client: httpx.AsyncClient,
    settings: Settings,
    launch_id: str,
    token: store.LaunchToken,
) -> store.LaunchToken:
    """Renew one launch's EHR access token, returning the record that replaced it.

    ``launch_id`` is unchanged by this and is never re-issued: it names the
    launch, not the token, so a client holding one is not made to re-learn it.
    Nothing here touches an ``encounters`` row either — an EHR token's lifetime
    and a visit's are independent, the same separation CLAUDE.md draws for the
    MedAuth session token.

    No audit row: obtaining a credential is not using it, which is the same test
    that keeps TASK-051's own two routes out of the audit trail. The PHI read
    this renewal is on the way to audits as it already did.

    Raises:
        ApiHTTPException: 401 when the grant is gone and the launch must be
            repeated; 502 or 504 when the answer never arrived.
    """
    if token.refresh_token is None:
        # An EHR that issued no refresh token, or a grant already refused. Either
        # way there is nothing to present, and the honest answer is the one
        # TASK-051 already documented.
        raise _launch_expired()

    credentials = require_credentials(settings, token.ehr_type)

    try:
        renewed = await refresh_access_token(
            http_client,
            token_endpoint=token.token_endpoint,
            credentials=credentials,
            refresh_token=token.refresh_token,
        )
    except TokenGrantRejected as exc:
        # The authorization server decided: this grant is not one it will
        # honour. The launch is over, and the record is rewritten without the
        # refused grant so the next request does not present it again.
        logger.warning(
            "Refusing to renew launch for %s: the grant was rejected (%s).",
            token.ehr_type.value,
            exc.oauth_error or "no error code",
        )
        await store.discard_refresh_grant(redis, launch_id, token)
        raise _launch_expired() from None
    except TokenEndpointUnavailable as exc:
        # We do not know whether the grant is still good, so nothing is written
        # and the launch is left intact. Ending it here would end a working
        # launch over a network hiccup.
        logger.warning(
            "Could not reach the token endpoint to renew a %s launch: %s",
            token.ehr_type.value,
            exc.detail,
        )
        raise _refresh_unavailable(timed_out=exc.timed_out) from None
    except TokenExchangeError as exc:
        # An answer arrived and was unusable — not JSON, or carrying no access
        # token. That is not the server refusing the grant, so the grant is kept
        # and this is reported as the same "we do not know" outcome.
        logger.warning(
            "Unusable renewal answer for a %s launch: %s", token.ehr_type.value, exc.detail
        )
        raise _refresh_unavailable(timed_out=False) from None

    updated = token.model_copy(
        update={
            "access_token": renewed.access_token,
            "access_token_expires_at": store.access_token_expiry(renewed.ttl_seconds),
            # Rotation: many servers issue a new refresh token and invalidate the
            # one just presented. Keeping the old value here would leave the
            # *second* renewal presenting a token the server has already thrown
            # away, while this one appeared to succeed.
            "refresh_token": renewed.refresh_token or token.refresh_token,
        }
    )
    await store.save_launch_token(
        redis,
        launch_id,
        updated,
        ttl_seconds=store.record_ttl_seconds(
            updated, refresh_grant_ttl_seconds=settings.smart_launch_record_ttl_seconds
        ),
    )
    logger.info("Renewed the EHR access token for a %s launch.", token.ehr_type.value)
    return updated


def _launch_expired() -> ApiHTTPException:
    """The 401 that says this launch is over and must be started again."""
    return ApiHTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        code=ERROR_CODE_LAUNCH_EXPIRED,
        message=(
            "This launch's EHR authorization is no longer valid. The SMART "
            "launch must be repeated from the EHR."
        ),
    )


def _refresh_unavailable(*, timed_out: bool) -> ApiHTTPException:
    """The 502/504 that says renewal failed without settling the grant's fate."""
    return ApiHTTPException(
        status_code=(status.HTTP_504_GATEWAY_TIMEOUT if timed_out else status.HTTP_502_BAD_GATEWAY),
        code=ERROR_CODE_REFRESH_UNAVAILABLE,
        message=(
            "The EHR's authorization server could not be reached to renew this "
            "launch's access token. This is transient — retrying is reasonable."
        ),
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
            401: "This launch's EHR authorization is no longer valid; repeat the launch.",
            404: "No such SMART launch, or the EHR holds no such patient.",
            502: "The EHR, or its authorization server, was unreachable or unusable.",
            504: "The EHR or its authorization server did not answer in time.",
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
            401: "This launch's EHR authorization is no longer valid; repeat the launch.",
            404: "No such SMART launch, or the EHR holds no such encounter.",
            502: "The EHR, or its authorization server, was unreachable or unusable.",
            504: "The EHR or its authorization server did not answer in time.",
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
            401: "This launch's EHR authorization is no longer valid; repeat the launch.",
            404: "No such SMART launch, or the EHR holds no such encounter.",
            502: "The EHR, or its authorization server, was unreachable or unusable.",
            504: "The EHR or its authorization server did not answer in time.",
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

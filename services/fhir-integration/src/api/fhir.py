"""The launch-keyed read routes — reading the EHR, and reading what the launch stored.

TASK-052 added ``GET /fhir/patient/{id}/context`` and
``GET /fhir/encounter/{id}``, the first routes in this repository that read a
patient's chart and the first PHI accesses in this service; TASK-052b added
``GET /fhir/encounter/{id}/coverage-context``. TASK-051d added
``GET /fhir/launch-context``, the one route here that reads no chart: it returns
the SMART launch context the EHR handed us at callback time, out of this
service's own store. It is a PHI disclosure all the same, and audits like the
rest — a patient identifier is PHI whichever store it came out of.

**All of them are keyed on ``launch_id``, carried in the ``X-MedAuth-Launch-Id``
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
launch record, and the handler asks ``get_adapter()`` for whatever answers. No
route here imports a concrete adapter, and none branches on which EHR it is
talking to.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Final

import httpx
from fastapi import APIRouter, Depends, Header, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

from api_envelope import ApiHTTPException, ApiResponse, error_responses
from fhir_types import Encounter
from hipaa_logger import AuditAction
from src.adapters import get_adapter
from src.adapters.base import PATIENT_SEARCH_LIMIT, EHRAdapter
from src.adapters.covermymeds import (
    CoverMyMedsClient,
    CoverMyMedsNotConfigured,
)
from src.adapters.errors import (
    FHIRAuthorizationExpired,
    FHIRMalformedResponse,
    FHIRResourceNotFound,
    FHIRUpstreamUnavailable,
)
from src.adapters.models import (
    ClinicalNoteContent,
    CoverageInfo,
    EncounterCoverageContext,
    NoteCode,
    PatientContext,
    PatientSearchResults,
    PriorAuthContent,
    PriorAuthEvidence,
    PriorAuthProcedure,
    SubmissionMethod,
    SubmissionOutcome,
)
from src.adapters.pas_bundle import PriorAuthNotSubmittable
from src.api.dependencies import (
    get_app_settings,
    get_http_client,
    get_redis,
    require_credentials,
)
from src.audit import (
    RESOURCE_TYPE_DOCUMENT_REFERENCE,
    RESOURCE_TYPE_ENCOUNTER,
    RESOURCE_TYPE_PATIENT,
    RESOURCE_TYPE_PRIOR_AUTH_REQUEST,
    audit_ehr_read,
    audit_ehr_write,
)
from src.config import Settings
from src.notes_client import NoteNotFound, NotesClient, NoteServiceError
from src.prior_auth_client import (
    PriorAuthAlreadySubmitted,
    PriorAuthClient,
    PriorAuthRequestNotFound,
    PriorAuthServiceError,
)
from src.providers_client import ProvidersClient, ProviderServiceError
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

#: TASK-053's own outcomes. They are separate from the ``FHIR_`` codes above
#: because they are facts about *our* services and this repository's rules,
#: not about an EHR: an operator reading one should not go looking at a
#: vendor's server.
ERROR_CODE_NOTE_NOT_FOUND: Final = "NOTE_NOT_FOUND"
ERROR_CODE_NOTE_SERVICE_UNAVAILABLE: Final = "NOTE_SERVICE_UNAVAILABLE"
ERROR_CODE_NOTE_ALREADY_WRITTEN: Final = "NOTE_ALREADY_WRITTEN_TO_EHR"
ERROR_CODE_ENCOUNTER_NOT_LINKED: Final = "ENCOUNTER_NOT_LINKED_TO_EHR"
#: The one outcome where the EHR *did* accept the document. Named distinctly
#: so a client cannot treat it as an ordinary failure and retry into a second
#: copy on the chart.
ERROR_CODE_RECORD_FAILED: Final = "EHR_NOTE_RECORD_FAILED"

#: TASK-054's outcomes, on the same terms as TASK-053's above: facts about our
#: own services and rules rather than about an EHR.
ERROR_CODE_PRIOR_AUTH_NOT_FOUND: Final = "PRIOR_AUTH_NOT_FOUND"
ERROR_CODE_PRIOR_AUTH_SERVICE_UNAVAILABLE: Final = "PRIOR_AUTH_SERVICE_UNAVAILABLE"
ERROR_CODE_PRIOR_AUTH_ALREADY_SUBMITTED: Final = "PRIOR_AUTH_ALREADY_SUBMITTED"
ERROR_CODE_PRIOR_AUTH_NOT_SUBMITTABLE: Final = "PRIOR_AUTH_NOT_SUBMITTABLE"
ERROR_CODE_PRIOR_AUTH_PATH_NOT_CONFIGURED: Final = "PRIOR_AUTH_PATH_NOT_CONFIGURED"
#: The one outcome where the payer *did* take the request. Named distinctly so a
#: client cannot treat it as an ordinary failure and retry into a second review.
ERROR_CODE_PRIOR_AUTH_RECORD_FAILED: Final = "PRIOR_AUTH_RECORD_FAILED"


class LaunchContextData(BaseModel):
    """The SMART launch context one launch carried. TASK-051d.

    **It names its fields rather than serialising ``LaunchToken``.** That record
    also holds the EHR access token, the refresh token that renews it and the
    granted scope, and a response model derived from it would start disclosing
    whatever field the next task adds to storage. Naming the fields explicitly
    means the next one can only be disclosed by someone deciding to disclose it.

    ``launch_id`` is deliberately not echoed back. The caller sent it in the
    request header, so it learns nothing from seeing it again — and it resolves
    to an EHR access token, which is the class of value this service already
    keeps out of URLs and logs. There is no reason to put a capability handle in
    one more place.
    """

    patient_id: str | None = Field(
        description=(
            "The patient the EHR launched us for, as an id on that EHR. Null "
            "for a launch that carried no patient context at all."
        ),
    )
    encounter_id: str | None = Field(
        description=(
            "The encounter the EHR launched us from, as an id on that EHR. Null "
            "for a standalone launch, which has a patient and no encounter — "
            "not an error, and not a reason to repeat the launch."
        ),
    )
    provider_id: str | None = Field(
        default=None,
        description=(
            "The provider who authorized this launch, as a UUID this system "
            "minted for them — what POST /sessions/start takes. Null when the "
            "EHR did not say who launched us, could not prove it, or when the "
            "registry could not be reached."
        ),
    )


async def get_launch_record(
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
) -> store.LaunchToken:
    """Load the launch's stored record, renewing its access token if it is stale.

    Split out from ``get_ehr_adapter`` by TASK-051c, which needs a second thing
    off the same record — the actor that launch was authorized by. FastAPI
    caches a dependency's result within one request, so both readers share a
    single Redis load and a single renewal rather than racing to renew twice.

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

    return token


async def get_audit_actor(
    token: Annotated[store.LaunchToken, Depends(get_launch_record)],
) -> str | None:
    """Return the provider who authorized this launch, for the audit row.

    An absolute ``Practitioner`` reference resolved from a **verified**
    ``id_token`` at callback time (TASK-051c), or ``None`` when the EHR did not
    say who launched us or could not prove it. ``None`` is written as a null
    actor, never replaced by an invented one.

    It is deliberately not an ``actor_id``: a ``Practitioner`` id is usually not
    a UUID, and ``audit_log`` keeps an EHR-asserted actor in a column of its own.
    See CLAUDE.md, "The EHR-asserted actor is its own column".
    """
    return token.fhir_practitioner_ref


async def get_ehr_adapter(
    token: Annotated[store.LaunchToken, Depends(get_launch_record)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> EHRAdapter:
    """Resolve the launch's stored token into an adapter for that EHR.

    Everything about loading that record — the 404, and the proactive token
    renewal — belongs to ``get_launch_record`` above. What is left here is
    choosing the adapter, which is the part a route actually asked for.

    The vendor comes out of the record and is never a request parameter, so
    nothing in this module imports a concrete adapter or branches on which EHR
    answered.

    **The CoverMyMeds client is built here and handed to the factory**, which
    gives it to the one adapter that submits through it (TASK-054). This function
    is where a request's configuration is already in scope, and the factory is
    where knowledge of which vendor needs it belongs — so no route learns that
    Athenahealth is different, which is the property the adapter layer exists
    for.
    """
    key = settings.covermymeds_api_key
    return get_adapter(
        token.ehr_type,
        token.fhir_base_url,
        token.access_token,
        http_client,
        covermymeds=CoverMyMedsClient(
            settings.covermymeds_base_url,
            key.get_secret_value() if key else "",
            http_client,
        ),
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
    actor: Annotated[str | None, Depends(get_audit_actor)],
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
        fhir_practitioner_ref=actor,
    )
    return ApiResponse[PatientContext](data=context)


@router.get(
    "/patient/search",
    response_model=ApiResponse[PatientSearchResults],
    status_code=status.HTTP_200_OK,
    summary="Find patients by name, for a launch that named none",
    response_description="The matching patients, and whether there were more.",
    responses=error_responses(
        401,
        404,
        422,
        502,
        504,
        descriptions={
            401: "This launch's EHR authorization is no longer valid; repeat the launch.",
            404: "No such SMART launch.",
            502: "The EHR, or its authorization server, was unreachable or unusable.",
            504: "The EHR or its authorization server did not answer in time.",
        },
    ),
)
async def search_patients(
    query: Annotated[
        str,
        Query(
            min_length=1,
            max_length=100,
            description="The patient's name, as the provider typed it.",
        ),
    ],
    adapter: Annotated[EHRAdapter, Depends(get_ehr_adapter)],
    actor: Annotated[str | None, Depends(get_audit_actor)],
    birth_date: Annotated[
        str | None,
        Query(
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="An optional YYYY-MM-DD date of birth, to narrow the search.",
        ),
    ] = None,
) -> ApiResponse[PatientSearchResults]:
    """Return the patients matching a name, for a launch the EHR named none in.

    TASK-025b. **This is the standalone-launch half of identifying a patient, and
    it does not replace ``GET /fhir/launch-context``.** After an EHR launch the
    EHR has already said who is in the room and TASK-051 stored the answer;
    searching there would be asking a question we hold the answer to, and would
    let a provider start a visit against a different patient than the chart in
    front of them. A client takes the launch context when there is one and comes
    here only when there is not.

    **Matching nobody is a 200 with an empty list, never a 404.** "The EHR holds
    nobody by that name" and "no such SMART launch" are different facts, and a
    404 here would tell a client to repeat a launch that is working. The 404 this
    route can answer is the launch one, from ``get_launch_record``.

    **More matches than fit are reported, never silently dropped.** A provider
    shown five of two hundred Smiths and not told so will conclude the patient
    they want is not in the system. Same rule as the transcript-limit one in
    CLAUDE.md — report reduced coverage rather than truncating in silence.

    This is a PHI read, and it writes **one ``READ_PATIENT`` row per match the
    response discloses** rather than one per call. A search that returned eight
    patients disclosed eight identifiers, including seven belonging to people who
    are not the patient in the room; a single row naming none of them would make
    those seven disclosures invisible to the one query the audit table exists to
    answer. A search that matched nothing discloses nothing and writes no row, on
    the same terms as a launch that carried no patient.
    """
    try:
        results = await adapter.search_patients(
            query, birth_date=birth_date, limit=PATIENT_SEARCH_LIMIT
        )
    except Exception as exc:
        raise _as_api_error(exc) from exc

    if results.truncated:
        # The count, never the query — a name is patient content and this is an
        # operational log. The client is told through `truncated`; this line is
        # so an operator can see the cap being hit routinely and reconsider it.
        logger.info("A patient search matched more than the %d returned.", PATIENT_SEARCH_LIMIT)

    for match in results.matches:
        await audit_ehr_read(
            action=AuditAction.READ_PATIENT,
            resource_type=RESOURCE_TYPE_PATIENT,
            resource_id=match.patient_id,
            fhir_practitioner_ref=actor,
        )
    return ApiResponse[PatientSearchResults](data=results)


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
    actor: Annotated[str | None, Depends(get_audit_actor)],
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
        fhir_practitioner_ref=actor,
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
    actor: Annotated[str | None, Depends(get_audit_actor)],
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
        fhir_practitioner_ref=actor,
    )
    return ApiResponse[EncounterCoverageContext](data=context)


async def get_providers_client(
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> ProvidersClient:
    """Build the client this service resolves providers through.

    The same shared, pooled HTTP client and the same per-call timeout as
    ``get_notes_client``: both call the same service, and one of them growing its
    own transport would be the "adapter builds its own client" mistake one layer
    up.
    """
    return ProvidersClient(
        settings.track_a_clinical_url,
        http_client,
        settings.track_a_clinical_timeout_seconds,
    )


async def _resolve_provider(providers: ProvidersClient, actor: str | None) -> str | None:
    """Turn this launch's verified practitioner reference into a ``provider_id``.

    **An unverified actor resolves to nothing.** ``actor`` is ``None`` when the
    EHR sent no ``id_token``, published no keys, or signed something that did not
    check out (TASK-051c). Registering a provider from a claim we could not
    verify would put a fabricated identity in the one column an auditor reads to
    answer "who saw this patient" — the same fabrication the null-over-invention
    rule refuses one table over.

    **A registry that cannot be reached is a null, not a failed launch.** The
    patient half of this response is what the caller mainly came for, and it was
    read from a record this service already holds; failing the whole route
    because a sibling service is restarting would make a working launch look
    expired. The client sees a null ``provider_id`` and cannot start a visit,
    which is the honest outcome and is recoverable by retrying.
    """
    if actor is None:
        return None
    try:
        return await providers.resolve(actor)
    except ProviderServiceError as exc:
        # Not the reference, which names an individual clinician.
        logger.warning("Could not resolve this launch's provider: %s", exc.detail)
        return None


@router.get(
    "/launch-context",
    response_model=ApiResponse[LaunchContextData],
    status_code=status.HTTP_200_OK,
    summary="Read the SMART launch context this launch carried",
    response_description="The patient and encounter the EHR launched us for.",
    responses=error_responses(
        401,
        404,
        422,
        502,
        504,
        descriptions={
            401: "This launch's EHR authorization is no longer valid; repeat the launch.",
            404: "No such SMART launch.",
            502: "The EHR's authorization server was unreachable or unusable.",
            504: "The EHR's authorization server did not answer in time.",
        },
    ),
)
async def read_launch_context(
    token: Annotated[store.LaunchToken, Depends(get_launch_record)],
    actor: Annotated[str | None, Depends(get_audit_actor)],
    providers: Annotated[ProvidersClient, Depends(get_providers_client)],
) -> ApiResponse[LaunchContextData]:
    """Return the patient and encounter the EHR named when it launched us.

    TASK-051d. ``GET /fhir/callback`` withholds these deliberately — a credential
    exchange is not the place to start handing out patient identifiers — so
    until this route existed a client that had completed a launch knew it had a
    launch and did not know who it was for, while ``POST /sessions/start`` needs
    both identifiers. The only bridge was a human reading them out of the EHR's
    own screen.

    **This is not the patient search route and does not make that one
    unnecessary.** ``GET /fhir/patient/search`` (TASK-025b) answers a
    *standalone* launch, where nobody has told us who is in the room. This
    answers an *EHR* launch, where the EHR already did and TASK-051 stored the
    answer. Searching for a patient the EHR has already named would be asking a
    question we hold the answer to, and would let a provider start a visit
    against a different patient than the chart in front of them.

    **A launch that carried no encounter is a 200 with a null ``encounter_id``.**
    A standalone launch has a patient and no encounter; an EHR launch normally
    has both. Collapsing "this launch had no encounter context" into "no such
    launch" would tell a client to repeat a launch that is working perfectly —
    the same conflation this repository rejects when a payer's silence is read
    as a negative determination. A client given a null starts its session
    without one and leaves the payer columns NULL, which
    ``resolve_query_parameters()`` already reports per procedure.

    **provider_id is resolved here rather than sent by the client.** The
    launch record holds a verified Practitioner reference and
    POST /sessions/start needs a UUID; the registry in track-a-clinical
    maps between them. Handing the reference to the client instead and letting it
    resolve its own provider would let an app assert a provider identity of its
    own, which is "the provider comes from the encounters row, never from the
    presented token's claim" applied one step earlier. See CLAUDE.md, "Provider
    identity — the registry that resolves an EHR practitioner".

    **It reads no chart, and it is still a PHI disclosure.** Which patient a
    provider was launched for is PHI whether it was read from the EHR just now
    or from our own Redis, so the route audits as ``READ_PATIENT`` like any
    other. A vocabulary member of its own was considered and rejected:
    ``AuditAction`` exists so that "who accessed patient X" is one query, and a
    second spelling for reading the same identifier out of a different store
    would fragment that answer rather than sharpen it.

    Renewing the EHR access token is not this route's business, but it happens
    anyway, in ``get_launch_record`` — which is why a 401, 502 or 504 is
    reachable here from a route that spends no token. That is the right trade:
    one loading path means a launch whose grant is gone answers the same way
    everywhere, rather than this route alone handing back a patient identifier
    under a launch that can no longer read the chart it names.
    """
    context = LaunchContextData(
        patient_id=token.patient_id,
        encounter_id=token.encounter_id,
        provider_id=await _resolve_provider(providers, actor),
    )

    # No patient, nothing disclosed, no row. A launch may carry no patient
    # context at all, and an audit row naming no resource would record an access
    # that did not happen — the same reason a failed read writes none.
    if context.patient_id is not None:
        await audit_ehr_read(
            action=AuditAction.READ_PATIENT,
            resource_type=RESOURCE_TYPE_PATIENT,
            resource_id=context.patient_id,
            fhir_practitioner_ref=actor,
        )
    return ApiResponse[LaunchContextData](data=context)


class WriteNoteRequest(BaseModel):
    """Body of ``POST /fhir/notes``.

    **One field, on purpose.** Everything else the write needs — the note's text,
    its codes, the chart entry it belongs on and the patient it is about — is
    read server-side from ``track-a-clinical``. A browser that posted the note
    body would save a round trip and produce no ``READ_NOTE`` row anywhere, which
    is the half that settles it. See CLAUDE.md, "Writing clinical data out to the
    EHR".

    ``session_id``, not ``launch_id`` and not ``ehr_encounter_id``: the launch is
    already carried in the header where every route here takes it, and the chart
    entry is a fact about the encounter that a client should not be asserting.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID = Field(
        description=(
            "The encounter session whose note is being filed. Names one visit in "
            "*our* namespace; the chart entry it corresponds to is resolved "
            "server-side."
        ),
    )


class WrittenNoteData(BaseModel):
    """What a successful write-back reports.

    The session is echoed so a caller with several in flight can match a response
    to its request, and the document id is what the chart now holds.
    """

    session_id: uuid.UUID = Field(description="The session whose note was filed.")
    ehr_document_ref_id: str = Field(
        description="The id of the DocumentReference the EHR created.",
    )


async def get_notes_client(
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> NotesClient:
    """Build the client this service reads notes through.

    The HTTP client is the process-wide pooled one, exactly as it is for an
    adapter and for the same reasons.
    """
    return NotesClient(
        settings.track_a_clinical_url,
        http_client,
        settings.track_a_clinical_timeout_seconds,
    )


def _note_service_error(exc: NoteServiceError) -> ApiHTTPException:
    """Map a failure of *our own* note service onto an envelope outcome.

    Deliberately not folded into ``_as_api_error``. "The EHR could not be
    reached" and "our note service could not be reached" are different facts
    about different systems, and an operator reading a 502 has to be able to tell
    which one to go and look at.
    """
    if isinstance(exc, NoteNotFound):
        return ApiHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ERROR_CODE_NOTE_NOT_FOUND,
            message=(
                "No such session, or no note has been generated for it yet. The "
                "second is the ordinary state for a few seconds after a visit ends."
            ),
        )
    return ApiHTTPException(
        status_code=(
            status.HTTP_504_GATEWAY_TIMEOUT if exc.timed_out else status.HTTP_502_BAD_GATEWAY
        ),
        code=ERROR_CODE_NOTE_SERVICE_UNAVAILABLE,
        message=(
            "The note service could not be reached. Nothing was written to the "
            "EHR; this is transient and retrying is reasonable."
        ),
    )


@router.post(
    "/notes",
    response_model=ApiResponse[WrittenNoteData],
    status_code=status.HTTP_201_CREATED,
    summary="File a session's SOAP note to the EHR",
    response_description="The DocumentReference the EHR created for this note.",
    responses=error_responses(
        401,
        404,
        409,
        422,
        502,
        504,
        descriptions={
            401: "This launch's EHR authorization is no longer valid; repeat the launch.",
            404: (
                "No such SMART launch, no such session, or no note has been "
                "generated for that session yet."
            ),
            409: (
                "This note has already been filed to the EHR "
                "(`NOTE_ALREADY_WRITTEN_TO_EHR`). Refused before the EHR is "
                "called: a second document would be a duplicate entry on a "
                "patient's chart."
            ),
            422: (
                "The body is invalid, or the encounter carries no EHR encounter "
                "id (`ENCOUNTER_NOT_LINKED_TO_EHR`) — a visit started outside a "
                "SMART launch has no chart entry to file against."
            ),
            502: (
                "The EHR, its authorization server, or the note service was "
                "unreachable or unusable. `EHR_NOTE_RECORD_FAILED` is the one "
                "case where the document *was* created — see its message."
            ),
            504: "An upstream did not answer in time.",
        },
    ),
)
async def write_note_to_ehr(
    body: WriteNoteRequest,
    adapter: Annotated[EHRAdapter, Depends(get_ehr_adapter)],
    actor: Annotated[str | None, Depends(get_audit_actor)],
    notes: Annotated[NotesClient, Depends(get_notes_client)],
) -> ApiResponse[WrittenNoteData]:
    """File a generated SOAP note to the patient's chart as a ``DocumentReference``.

    **The first write to an EHR in this repository.** The cross-cutting rules it
    follows are in CLAUDE.md, "Writing clinical data out to the EHR"; what
    matters at this route is the order and what each failure means.

    The order is: refuse a repeat before anything happens, read the note, write
    it to the EHR, audit, then record the document id locally. The external write
    goes first deliberately. Both orders can fail in the middle, so the question
    is only which wreckage is findable — an EHR document with nothing here
    pointing at it can be found on the chart and reconciled, whereas a local row
    claiming a document exists when the write never happened is a silent lie no
    query can distinguish from success.

    **A repeat write is refused, not deduplicated.** Two ``DocumentReference``
    resources for one encounter is duplicate clinical documentation. The check
    here is the fast path; the guarantee is the conditional update behind
    ``PATCH /notes/{session_id}/ehr-reference``, which is what decides when two
    requests race.

    **Machine-suggested codes never reach the chart.** The filter is inside the
    document builder rather than here, so a vendor subclass that reuses it
    inherits the rule.

    This is a PHI disclosure and writes one ``WRITE_NOTE_TO_EHR`` row, after the
    EHR accepts the document and before the id is recorded — so the trail exists
    even when the recording step is the thing that failed.
    """
    session_id = str(body.session_id)

    try:
        reference = await notes.get_ehr_reference(session_id)
    except NoteServiceError as exc:
        raise _note_service_error(exc) from exc

    if reference.ehr_document_ref_id is not None:
        raise ApiHTTPException(
            status_code=status.HTTP_409_CONFLICT,
            code=ERROR_CODE_NOTE_ALREADY_WRITTEN,
            message=(
                "This note has already been filed to the EHR. Filing it again "
                "would put a second copy of one encounter's note on the chart."
            ),
        )

    if reference.ehr_encounter_id is None:
        raise ApiHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ERROR_CODE_ENCOUNTER_NOT_LINKED,
            message=(
                "This encounter has no EHR encounter id, so there is no chart "
                "entry to file the note against. It was started outside a SMART "
                "launch."
            ),
        )

    try:
        note = await notes.get_note(session_id)
    except NoteServiceError as exc:
        raise _note_service_error(exc) from exc

    content = ClinicalNoteContent(
        patient_id=reference.patient_fhir_id,
        encounter_id=reference.ehr_encounter_id,
        subjective=note.soap_subjective,
        objective=note.soap_objective,
        assessment=note.soap_assessment,
        plan=note.soap_plan,
        # Unfiltered on purpose: the builder filters, so no call site can forget.
        icd10_codes=[
            NoteCode(code=code.code, display=code.display, source=code.source)
            for code in note.icd10_codes or ()
        ],
        reviewed_by_provider=note.reviewed_by_provider,
    )

    try:
        document_id = await adapter.write_clinical_note(content)
    except Exception as exc:
        raise _as_api_error(exc) from exc

    await audit_ehr_write(
        action=AuditAction.WRITE_NOTE_TO_EHR,
        resource_type=RESOURCE_TYPE_DOCUMENT_REFERENCE,
        resource_id=document_id,
        session_id=session_id,
        fhir_practitioner_ref=actor,
    )

    try:
        await notes.record_ehr_document_ref(session_id, document_id)
    except NoteServiceError as exc:
        # The document exists on the chart. Reporting this as a plain failure
        # would invite a retry that files a second one, so the id is named in the
        # message and in the log: this needs reconciling, not repeating.
        logger.error(
            "Filed DocumentReference %s to the EHR but could not record it against "
            "the note: %s. The document exists and needs reconciling by hand.",
            document_id,
            exc.detail,
        )
        raise ApiHTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code=ERROR_CODE_RECORD_FAILED,
            message=(
                f"The note was filed to the EHR as DocumentReference/{document_id}, "
                "but recording it here failed. The document exists — do not retry "
                "this write, or a second copy will be filed."
            ),
        ) from exc

    return ApiResponse[WrittenNoteData](
        data=WrittenNoteData(session_id=body.session_id, ehr_document_ref_id=document_id)
    )


class SubmitPriorAuthRequest(BaseModel):
    """Body of ``POST /fhir/prior-auth``.

    **One field, on purpose**, exactly as ``POST /fhir/notes`` carries only a
    session. Everything the submission needs — the procedures, the diagnoses, the
    clinical evidence, the payer and the two EHR identifiers — is read
    server-side from ``track-a-clinical``. A client that posted the bundle back
    would produce no ``READ_PRIOR_AUTH`` row anywhere, which is the half that
    settles it, and it would put a payer submission's payload under the control
    of the least trusted participant in it. See CLAUDE.md, "Writing clinical data
    out to the EHR".

    ``request_id``, not ``session_id``: one encounter can carry several
    prior-authorization requests, so a session would not name one.

    **This route submits a request; it does not choose one and does not assemble
    one.** Assembly is TASK-060 and the routing that decides whether a payer
    takes FHIR PAS at all is TASK-061. By the time this is called, that choice
    has been made.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID = Field(
        description=(
            "The prior-authorization request to submit. Names one row of "
            "`prior_auth_requests`; what is in it is resolved server-side."
        ),
    )


class PriorAuthSubmissionData(BaseModel):
    """What a successful submission reports.

    ``outcome`` is here rather than only in the database because a caller has to
    be able to tell a queued request from an adjudicated one without a second
    round trip — and because a response carrying only a reference number would
    let a client show "submitted" for a request the payer refused.
    """

    request_id: uuid.UUID = Field(description="The request that was submitted.")
    outcome: SubmissionOutcome = Field(
        description=(
            "What the payer said. `complete` means it adjudicated the request "
            "and says nothing about which way; `queued` means it accepted and "
            "has not decided; `error` means it refused to process at all."
        ),
    )
    payer_reference_number: str | None = Field(
        default=None,
        description=(
            "The payer's reference for the submission, when it gave one. "
            "Legitimately absent on a queued answer."
        ),
    )
    submission_method: SubmissionMethod = Field(
        description="Which path transmitted it — FHIR PAS, or CoverMyMeds."
    )


async def get_prior_auth_client(
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> PriorAuthClient:
    """Build the client this service reads prior-authorization requests through.

    The HTTP client is the process-wide pooled one, exactly as it is for an
    adapter and for the same reasons.
    """
    return PriorAuthClient(
        settings.track_a_clinical_url,
        http_client,
        settings.track_a_clinical_timeout_seconds,
    )


def _prior_auth_service_error(exc: PriorAuthServiceError) -> ApiHTTPException:
    """Map a failure of *our own* service onto an envelope outcome.

    Deliberately not folded into ``_as_api_error``, on the same terms as the note
    write-back's mapping: "the payer could not be reached" and "our own service
    could not be reached" are different facts about different systems, and an
    operator reading a 502 has to know which one to go and look at.
    """
    if isinstance(exc, PriorAuthRequestNotFound):
        return ApiHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ERROR_CODE_PRIOR_AUTH_NOT_FOUND,
            message="No such prior authorization request.",
        )
    if isinstance(exc, PriorAuthAlreadySubmitted):
        return ApiHTTPException(
            status_code=status.HTTP_409_CONFLICT,
            code=ERROR_CODE_PRIOR_AUTH_ALREADY_SUBMITTED,
            message=(
                "This prior authorization has already been submitted. Submitting "
                "it again would ask the payer to open a second review of one "
                "request."
            ),
        )
    return ApiHTTPException(
        status_code=(
            status.HTTP_504_GATEWAY_TIMEOUT if exc.timed_out else status.HTTP_502_BAD_GATEWAY
        ),
        code=ERROR_CODE_PRIOR_AUTH_SERVICE_UNAVAILABLE,
        message=(
            "The clinical service could not be reached. Nothing was submitted to "
            "a payer; this is transient and retrying is reasonable."
        ),
    )


@router.post(
    "/prior-auth",
    response_model=ApiResponse[PriorAuthSubmissionData],
    status_code=status.HTTP_201_CREATED,
    summary="Submit a prior authorization to the payer",
    response_description="What the payer said, and which path submitted it.",
    responses=error_responses(
        401,
        404,
        409,
        422,
        502,
        504,
        descriptions={
            401: "This launch's EHR authorization is no longer valid; repeat the launch.",
            404: (
                "No such SMART launch, no such prior authorization request, or "
                "the endpoint does not implement `Claim/$submit`."
            ),
            409: (
                "This request has already been submitted "
                "(`PRIOR_AUTH_ALREADY_SUBMITTED`). Refused before the payer is "
                "called: a payer receiving one request twice may open two reviews."
            ),
            422: (
                "The body is invalid, or the request cannot be made into a "
                "conformant submission (`PRIOR_AUTH_NOT_SUBMITTABLE`) — no "
                "procedure to request, or no verified provider to ask on behalf "
                "of. `PRIOR_AUTH_PATH_NOT_CONFIGURED` means this EHR submits "
                "through CoverMyMeds and nothing configured it."
            ),
            502: (
                "The payer, its endpoint, or the clinical service was "
                "unreachable or unusable. `PRIOR_AUTH_RECORD_FAILED` is the one "
                "case where the payer *did* accept the request — see its message."
            ),
            504: "An upstream did not answer in time.",
        },
    ),
)
async def submit_prior_auth(
    body: SubmitPriorAuthRequest,
    adapter: Annotated[EHRAdapter, Depends(get_ehr_adapter)],
    actor: Annotated[str | None, Depends(get_audit_actor)],
    requests: Annotated[PriorAuthClient, Depends(get_prior_auth_client)],
) -> ApiResponse[PriorAuthSubmissionData]:
    """Submit an assembled prior-authorization request to the payer.

    **The second outbound writer**, and it inherits CLAUDE.md's "Writing clinical
    data out to the EHR" rules rather than re-deciding them: the ``source``
    filter on any codes leaving this system, its own audit action, a row in each
    service that acted, the external write first and the local record second, and
    the refusal to submit twice.

    The order is: refuse a repeat before anything happens, read the request,
    submit it, audit, then record the result locally. The external write goes
    first deliberately — an authorization the payer holds with nothing here
    pointing at it can be reconciled by its reference number, whereas a local row
    claiming a submission that never happened is a silent lie no query can
    distinguish from success.

    **Which path submits is the adapter's business, not this route's.** A payer
    on an EHR with no FHIR PAS support goes through CoverMyMeds, and nothing here
    knows or asks which EHR answered.

    This is a PHI disclosure to a third party and writes one
    ``SUBMIT_PRIOR_AUTH`` row, after the payer answers and before the result is
    recorded — so the trail exists even when the recording step is what failed.
    """
    request_id = str(body.request_id)

    try:
        stored = await requests.get_request(request_id)
    except PriorAuthServiceError as exc:
        raise _prior_auth_service_error(exc) from exc

    if stored.submitted_at is not None:
        raise ApiHTTPException(
            status_code=status.HTTP_409_CONFLICT,
            code=ERROR_CODE_PRIOR_AUTH_ALREADY_SUBMITTED,
            message=(
                "This prior authorization has already been submitted. Submitting "
                "it again would ask the payer to open a second review of one "
                "request."
            ),
        )

    content = PriorAuthContent(
        request_id=request_id,
        patient_id=stored.patient_fhir_id,
        encounter_id=stored.ehr_encounter_id or "",
        # The provider the EHR asserted at launch and this service verified.
        # Never encounters.provider_id, a UUID that identifies nobody to a payer.
        provider_reference=actor,
        payer_name=stored.payer_name,
        coverage=CoverageInfo(
            payer=stored.payer_name,
            plan_type=stored.insurance_plan_type,
            member_id=stored.insurance_member_id,
        ),
        procedures=[
            PriorAuthProcedure(cpt_code=procedure.cpt_code, description=procedure.description)
            for procedure in stored.procedures or ()
        ],
        # Unfiltered on purpose: the builder filters, so no call site can forget.
        icd10_codes=[
            NoteCode(code=code.code, display=code.display, source=code.source)
            for code in stored.diagnoses or ()
        ],
        clinical_evidence=[
            PriorAuthEvidence(text=evidence.text, criterion=evidence.criterion)
            for evidence in stored.clinical_evidence or ()
        ],
    )

    try:
        submission = await adapter.submit_prior_auth(content)
    except PriorAuthNotSubmittable as exc:
        # Refused before anything left this system: a required fact is missing,
        # and the alternative is asserting one nobody stated.
        raise ApiHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ERROR_CODE_PRIOR_AUTH_NOT_SUBMITTABLE,
            message=f"This request cannot be submitted: {exc.reason}.",
        ) from exc
    except CoverMyMedsNotConfigured as exc:
        raise ApiHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ERROR_CODE_PRIOR_AUTH_PATH_NOT_CONFIGURED,
            message=(
                "This EHR submits prior authorizations through CoverMyMeds, and "
                "that path is not configured. Nothing was submitted."
            ),
        ) from exc
    except Exception as exc:
        raise _as_api_error(exc) from exc

    await audit_ehr_write(
        action=AuditAction.SUBMIT_PRIOR_AUTH,
        resource_type=RESOURCE_TYPE_PRIOR_AUTH_REQUEST,
        resource_id=request_id,
        session_id=stored.session_id,
        fhir_practitioner_ref=actor,
    )

    try:
        await requests.record_submission(
            request_id,
            submission_method=submission.submission_method,
            outcome=submission.outcome,
            payer_reference_number=submission.payer_reference_number,
        )
    except PriorAuthServiceError as exc:
        # The payer has the request. Reporting this as a plain failure would
        # invite a retry that submits a second one, so the reference number is
        # named in the message and in the log: this needs reconciling, not
        # repeating.
        logger.error(
            "Submitted prior auth request %s to the payer (%s, reference %s) but could "
            "not record it: %s. The submission exists and needs reconciling by hand.",
            request_id,
            submission.submission_method.value,
            submission.payer_reference_number or "none given",
            exc.detail,
        )
        raise ApiHTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code=ERROR_CODE_PRIOR_AUTH_RECORD_FAILED,
            message=(
                "The prior authorization was submitted to the payer"
                + (
                    f" (reference {submission.payer_reference_number})"
                    if submission.payer_reference_number
                    else ""
                )
                + ", but recording it here failed. The submission exists — do not "
                "retry, or the payer will receive it twice."
            ),
        ) from exc

    return ApiResponse[PriorAuthSubmissionData](
        data=PriorAuthSubmissionData(
            request_id=body.request_id,
            outcome=submission.outcome,
            payer_reference_number=submission.payer_reference_number,
            submission_method=submission.submission_method,
        )
    )

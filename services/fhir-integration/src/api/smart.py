"""``GET /fhir/launch`` and ``GET /fhir/callback`` — the SMART on FHIR OAuth flow.

TASK-051. These two routes are the whole of the launch: the first sends a
provider's browser to their EHR's authorization server, the second takes the
code that comes back and turns it into an EHR access token this service can use.

**Neither route touches PHI, so neither audits.** Obtaining a credential is not
using it. Judged by the same test as TASK-024's ``resolve_query_parameters()``
SELECT: the first PHI access in this service is TASK-052's resource fetches, and
that is where ``audit_log()`` starts. Both routes log at INFO through the
standard logger per Known Constraints #6, which is where the operational trace
belongs when there is no audit row to write.

**Neither route audits, and TASK-051c does not change that.** The callback now
verifies an ``id_token`` and records who authorized the launch, but obtaining a
credential — or learning whose it is — is still not using one. That actor is
written on every audit row the PHI routes produce, which is where it belongs.

**What must never reach a log line here**: the ``launch`` parameter, the
authorization ``code``, the ``state``, the ``code_verifier``, the client secret,
the access token, the ``id_token`` and any claim read out of it, and the SMART
launch context the token response carries. The
``iss`` is logged as a **host**, never in full — a launch URL can carry context
in its query string. What is left to log is the vendor key and the launch's
outcome, which is what an operator actually needs.

**The identifier this flow hands back is ``launch_id``, never ``session_id``.**
Two different things with two different lifetimes; at callback time no encounter
exists to key a token on. Settled in CLAUDE.md, "A SMART launch is not an
encounter session".
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

import httpx
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from api_envelope import ApiHTTPException, ApiResponse, error_responses
from src.adapters.factory import EHRType, detect_ehr_from_issuer
from src.api.dependencies import (
    get_app_settings,
    get_http_client,
    get_redis,
    require_credentials,
)
from src.config import Settings
from src.smart import store
from src.smart.discovery import DiscoveryError, fetch_smart_configuration
from src.smart.identity import resolve_launch_actor
from src.smart.issuer import issuer_host, normalize_fhir_base_url
from src.smart.oauth import (
    TokenExchangeError,
    authorization_redirect_url,
    exchange_code_for_token,
)
from src.smart.pkce import generate_code_verifier
from src.smart.store import LaunchToken, PendingLaunch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fhir", tags=["smart"])

ERROR_CODE_DISCOVERY_FAILED: Final = "SMART_DISCOVERY_FAILED"
ERROR_CODE_UNKNOWN_STATE: Final = "SMART_UNKNOWN_STATE"
ERROR_CODE_AUTHORIZATION_DENIED: Final = "SMART_AUTHORIZATION_DENIED"
ERROR_CODE_TOKEN_EXCHANGE_FAILED: Final = "SMART_TOKEN_EXCHANGE_FAILED"

#: Wording for the two statuses api-envelope has no generic text for. A launch
#: fails in two distinguishable ways and they are not the caller's fault in the
#: same way: 502 means the EHR did not answer usably, 500 means this deployment
#: is missing a registration.
_ERROR_DESCRIPTIONS: Final = {
    status.HTTP_500_INTERNAL_SERVER_ERROR: (
        "No SMART client is registered in this deployment for the EHR the issuer resolved to."
    ),
    status.HTTP_502_BAD_GATEWAY: (
        "The EHR's authorization server could not be reached, or answered in a way "
        "this service cannot use."
    ),
}


class LaunchSessionData(BaseModel):
    """What ``GET /fhir/callback`` hands back once a launch completes."""

    launch_id: str = Field(
        description=(
            "Names this SMART launch and the EHR access token it produced. It is "
            "NOT an encounter session_id: a launch precedes the visit and outlives "
            "individual encounters. See CLAUDE.md, 'A SMART launch is not an "
            "encounter session'."
        ),
    )
    ehr_type: EHRType = Field(
        description="The EHR vendor the issuer resolved to, or 'generic'.",
    )
    expires_in: int = Field(
        description="Seconds until the stored EHR access token expires.",
    )


@router.get(
    "/launch",
    status_code=status.HTTP_302_FOUND,
    summary="Begin a SMART on FHIR launch",
    response_class=RedirectResponse,
    responses={
        status.HTTP_302_FOUND: {
            "description": "Redirect to the EHR's authorization endpoint.",
        },
        **error_responses(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            status.HTTP_502_BAD_GATEWAY,
            descriptions=_ERROR_DESCRIPTIONS,
        ),
    },
)
async def smart_launch(
    redis: Annotated[Redis, Depends(get_redis)],
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    iss: Annotated[
        str,
        Query(
            min_length=1,
            description=(
                "The EHR's FHIR base URL. Identifies the vendor and is the `aud` "
                "the authorization request is bound to."
            ),
        ),
    ],
    launch: Annotated[
        str | None,
        Query(
            description=(
                "The EHR's opaque launch context. Present on an EHR launch and "
                "absent on a standalone launch, which is the only thing that "
                "distinguishes the two."
            ),
        ),
    ] = None,
) -> RedirectResponse:
    """Start a SMART launch and redirect the browser to the EHR.

    Resolves the vendor from ``iss``, reads that EHR's authorization and token
    endpoints from its `.well-known/smart-configuration` document, records the
    launch under `fhir_launch:{state}` with a fresh PKCE verifier, and redirects.

    Supports both launch types. An EHR launch arrives with ``launch`` and asks
    for the ``launch`` scope; a standalone launch arrives without one and asks
    for ``launch/patient`` instead, so the authorization server prompts for the
    patient the EHR would otherwise have named.

    Touches no PHI and writes no audit row — it obtains no patient data, only
    permission to ask for some later.
    """
    host = issuer_host(iss)
    # Normalised once, here, and nothing downstream sees the raw value: it is
    # appended to, sent as `aud`, stored, and handed to an HTTP client that logs
    # its own requests. See src/smart/issuer.py.
    fhir_base_url = normalize_fhir_base_url(iss)
    ehr_type = detect_ehr_from_issuer(iss)
    credentials = require_credentials(settings, ehr_type)

    try:
        configuration = await fetch_smart_configuration(http, fhir_base_url, issuer_host=host)
    except DiscoveryError as exc:
        logger.warning("SMART discovery failed for %s: %s", host, exc.detail)
        raise ApiHTTPException(
            status.HTTP_502_BAD_GATEWAY,
            ERROR_CODE_DISCOVERY_FAILED,
            str(exc),
        ) from None

    state = store.new_state()
    launch_id = store.new_launch_id()
    code_verifier = generate_code_verifier()
    is_ehr_launch = launch is not None

    await store.save_pending_launch(
        redis,
        state,
        PendingLaunch(
            launch_id=launch_id,
            iss=fhir_base_url,
            ehr_type=ehr_type,
            code_verifier=code_verifier,
            token_endpoint=configuration.token_endpoint,
            ehr_launch=is_ehr_launch,
            # From the same document as the endpoints above, for the same reason
            # the token endpoint is carried rather than rediscovered: the key
            # set an id_token is checked against must come from the document
            # that named the server issuing it (TASK-051c).
            oidc_issuer=configuration.issuer,
            jwks_uri=configuration.jwks_uri,
        ),
        ttl_seconds=settings.smart_launch_ttl_seconds,
    )

    redirect_to = authorization_redirect_url(
        authorization_endpoint=configuration.authorization_endpoint,
        credentials=credentials,
        redirect_uri=settings.smart_redirect_uri,
        scopes=settings.authorization_scopes(ehr_launch=is_ehr_launch),
        state=state,
        aud=fhir_base_url,
        code_verifier=code_verifier,
        launch=launch,
    )

    logger.info(
        "SMART %s launch started for %s (vendor %s)",
        "EHR" if is_ehr_launch else "standalone",
        host,
        ehr_type.value,
    )
    # 302 rather than FastAPI's default 307: this is a browser navigation to an
    # authorization endpoint, and the method must not be preserved across it.
    return RedirectResponse(redirect_to, status_code=status.HTTP_302_FOUND)


@router.get(
    "/callback",
    response_model=ApiResponse[LaunchSessionData],
    summary="Complete a SMART on FHIR launch",
    response_description="The launch_id naming this launch and its EHR access token.",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        status.HTTP_502_BAD_GATEWAY,
        descriptions={
            status.HTTP_400_BAD_REQUEST: (
                "The `state` is unknown, expired or already consumed, or the "
                "authorization server reported that the provider declined."
            ),
            **_ERROR_DESCRIPTIONS,
        },
    ),
)
async def callback(
    redis: Annotated[Redis, Depends(get_redis)],
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    state: Annotated[
        str,
        Query(min_length=1, description="The state this service issued on the redirect."),
    ],
    code: Annotated[str | None, Query(description="The authorization code.")] = None,
    error: Annotated[
        str | None,
        Query(description="An OAuth error code, when the authorization server refused."),
    ] = None,
) -> ApiResponse[LaunchSessionData]:
    """Exchange the authorization code for an EHR access token.

    Claims the launch record for this ``state`` — atomically, so a replayed
    callback finds nothing and cannot mint a second token — presents the PKCE
    verifier held with it, and stores the resulting token under
    `fhir_token:{launch_id}` for as long as the EHR says it lives.

    Also resolves who authorized the launch, from the ``id_token``'s verified
    ``fhirUser`` claim, and stores it on the record as the actor for every PHI
    read made under this launch (TASK-051c). That resolution never fails the
    launch: an EHR that sends no ``id_token``, publishes no keys, or sends one
    that does not verify leaves the actor unknown, which a null honestly
    records — and an *unverified* claim is never written in its place.

    Returns the ``launch_id``. It does not return the SMART launch context the
    token response carried, nor the resolved actor: those identifiers are stored
    for TASK-052, which audits when it reads them, and a credential exchange is
    not the place to start handing patient identifiers to a client.

    Touches no PHI and writes no audit row, for the same reason as the launch
    route above.
    """
    pending = await store.claim_launch(redis, state)
    if pending is None:
        # Unknown, expired and already-consumed are one answer on purpose: the
        # difference would tell a caller probing states which ones were real.
        logger.warning("Rejected SMART callback: no pending launch for the presented state")
        raise ApiHTTPException(
            status.HTTP_400_BAD_REQUEST,
            ERROR_CODE_UNKNOWN_STATE,
            "No pending launch matches the presented state",
        )

    host = issuer_host(pending.iss)

    if error is not None:
        logger.info("SMART launch declined at %s (%s)", host, error)
        raise ApiHTTPException(
            status.HTTP_400_BAD_REQUEST,
            ERROR_CODE_AUTHORIZATION_DENIED,
            f"The EHR's authorization server refused the launch ({error})",
        )

    if code is None:
        raise ApiHTTPException(
            status.HTTP_400_BAD_REQUEST,
            ERROR_CODE_AUTHORIZATION_DENIED,
            "The callback carried neither an authorization code nor an error",
        )

    credentials = require_credentials(settings, pending.ehr_type)

    try:
        # The token endpoint comes from the launch record, not from a second
        # discovery call: see PendingLaunch.token_endpoint.
        token = await exchange_code_for_token(
            http,
            token_endpoint=pending.token_endpoint,
            credentials=credentials,
            code=code,
            redirect_uri=settings.smart_redirect_uri,
            code_verifier=pending.code_verifier,
        )
    except TokenExchangeError as exc:
        logger.warning("Token exchange failed for %s: %s", host, exc.detail)
        raise ApiHTTPException(
            status.HTTP_502_BAD_GATEWAY, ERROR_CODE_TOKEN_EXCHANGE_FAILED, str(exc)
        ) from None

    # Who authorized this launch, if the EHR both told us and can prove it.
    # Never fails the launch: an unverifiable claim leaves the actor unknown,
    # which is what a null honestly records. TASK-051c.
    actor_reference = await resolve_launch_actor(
        http,
        id_token=token.id_token,
        jwks_uri=pending.jwks_uri,
        oidc_issuer=pending.oidc_issuer,
        audience=credentials.client_id,
        fhir_base_url=pending.iss,
        issuer_host=host,
    )

    launch_token = LaunchToken(
        ehr_type=pending.ehr_type,
        fhir_base_url=pending.iss,
        access_token=token.access_token,
        access_token_expires_at=store.access_token_expiry(token.ttl_seconds),
        # Carried from the launch record rather than rediscovered at renewal:
        # see PendingLaunch.token_endpoint, whose reasoning applies to a refresh
        # exactly as it does to this exchange.
        token_endpoint=pending.token_endpoint,
        refresh_token=token.refresh_token,
        patient_id=token.patient,
        encounter_id=token.encounter,
        scope=token.scope,
        fhir_practitioner_ref=actor_reference,
    )
    # The record outlives the access token when there is a grant to renew it
    # with, and expires with it when there is not. TASK-051b; see CLAUDE.md,
    # "The launch record outlives its access token".
    await store.save_launch_token(
        redis,
        pending.launch_id,
        launch_token,
        ttl_seconds=store.record_ttl_seconds(
            launch_token,
            refresh_grant_ttl_seconds=settings.smart_launch_record_ttl_seconds,
        ),
    )

    logger.info(
        "SMART launch completed for %s (vendor %s), token held for %ds",
        host,
        pending.ehr_type.value,
        token.ttl_seconds,
    )
    return ApiResponse[LaunchSessionData](
        data=LaunchSessionData(
            launch_id=pending.launch_id,
            ehr_type=pending.ehr_type,
            expires_in=token.ttl_seconds,
        )
    )

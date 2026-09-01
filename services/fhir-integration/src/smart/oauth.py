"""Building the authorization redirect and exchanging the code for a token.

Two halves of one conversation with the EHR's authorization server, kept
together because the parameters have to agree across them: the ``redirect_uri``
sent on the redirect is sent again on the exchange, and the ``code_verifier``
held back from the redirect is what the exchange presents.
"""

from __future__ import annotations

import logging
from typing import Final
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field, ValidationError

from src.config import ClientCredentials
from src.smart.pkce import CODE_CHALLENGE_METHOD, derive_code_challenge

logger = logging.getLogger(__name__)

#: How long to wait on a token exchange. As with discovery, a person is waiting
#: on a redirect; a round default rather than a measured one.
TOKEN_TIMEOUT_SECONDS: Final = 10.0

#: What an EHR gets when it returns no ``expires_in``. SMART on FHIR makes the
#: field optional, and a token record with no TTL would outlive the credential
#: it holds. Five minutes is deliberately short: it is a floor for a token whose
#: real lifetime we were not told, not a guess at what that lifetime is.
DEFAULT_TOKEN_TTL_SECONDS: Final = 300


class TokenResponse(BaseModel):
    """An EHR's answer to a token exchange.

    Extra fields are kept rather than rejected: SMART launch context arrives
    here as vendor-placed keys alongside the standard ones, and ``patient`` and
    ``encounter`` are read off it below.
    """

    model_config = {"extra": "allow"}

    access_token: str = Field(min_length=1)
    token_type: str = ""
    expires_in: int | None = None
    refresh_token: str | None = None
    scope: str | None = None
    patient: str | None = None
    encounter: str | None = None

    @property
    def ttl_seconds(self) -> int:
        """Return how long to hold this token, never longer than it lives."""
        if self.expires_in is not None and self.expires_in > 0:
            return self.expires_in
        return DEFAULT_TOKEN_TTL_SECONDS


class TokenExchangeError(RuntimeError):
    """The EHR refused or could not complete a token exchange.

    The message carries the authorization server's OAuth ``error`` code where
    there was one, because that is what distinguishes a misconfigured
    ``redirect_uri`` from an expired code. It never carries the response body,
    which holds the token on the success path and can hold echoes of the request
    on the failure path.

    **Two subclasses split what a refresh has to tell apart** — see
    ``TokenGrantRejected`` and ``TokenEndpointUnavailable``. The base class is
    still raised on its own, for an answer that was neither a refusal nor a
    failure to arrive: a body that is not JSON, or one carrying no access token.
    A code exchange treats all three the same way, because a launch that has not
    completed simply fails; only renewal has a live grant to protect.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Token exchange failed: {detail}")


class TokenGrantRejected(TokenExchangeError):
    """The authorization server answered, and refused.

    A 4xx from a token endpoint is a decision rather than an accident: the grant
    presented is not one this server will honour. For a refresh that means the
    grant is gone — revoked, or a refresh token already rotated out — and the
    launch must be repeated.

    ``oauth_error`` carries RFC 6749's machine-readable code where the server
    supplied one. It is kept as a field rather than only in the message because
    ``invalid_grant`` and ``invalid_client`` send whoever is debugging a launch
    to two different places: a dead grant, or a misconfigured registration.
    """

    def __init__(self, detail: str, *, oauth_error: str | None = None) -> None:
        self.oauth_error = oauth_error
        super().__init__(detail)


class TokenEndpointUnavailable(TokenExchangeError):
    """No usable answer arrived, so the grant's fate is unknown.

    A connect error, a read timeout, or a 5xx. This is a separate type rather
    than a message difference because reading it as a dead grant would end a
    working launch over a network hiccup — the same "silence is not a negative
    determination" error CLAUDE.md rejects in the CRD path, one layer down.
    """

    def __init__(self, detail: str, *, timed_out: bool = False) -> None:
        self.timed_out = timed_out
        super().__init__(detail)


def authorization_redirect_url(
    *,
    authorization_endpoint: str,
    credentials: ClientCredentials,
    redirect_uri: str,
    scopes: str,
    state: str,
    aud: str,
    code_verifier: str,
    launch: str | None,
) -> str:
    """Build the URL the browser is redirected to.

    Two parameters here are required by the standard and were missing from
    TASK-051's earlier text, which is why they are named rather than left to
    look incidental:

    * ``code_challenge`` / ``code_challenge_method`` — SMART on FHIR 2.0
      requires PKCE of every client. Only the challenge travels; the verifier
      stays in ``fhir_launch:{state}``.
    * ``aud`` — the FHIR base URL this app intends to call, which for a SMART
      launch is ``iss``. A conformant authorization server rejects a request
      without it, and it is what stops a code issued for one FHIR server being
      redeemed against another.

    Args:
        authorization_endpoint: From the EHR's discovery document.
        credentials: The registered client for this EHR.
        redirect_uri: Must match what the vendor's portal has registered.
        scopes: From ``Settings.authorization_scopes()``.
        state: This flow's CSRF token, and the key its record is held under.
        aud: The FHIR base URL — ``iss``.
        code_verifier: Held back; only its S256 challenge is sent.
        launch: The EHR's opaque launch context, on an EHR launch. ``None`` on a
            standalone launch, where there is none to pass on.

    Returns:
        The absolute URL to redirect the browser to.
    """
    params = {
        "response_type": "code",
        "client_id": credentials.client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "aud": aud,
        "code_challenge": derive_code_challenge(code_verifier),
        "code_challenge_method": CODE_CHALLENGE_METHOD,
    }
    if launch is not None:
        params["launch"] = launch

    separator = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{separator}{urlencode(params)}"


async def exchange_code_for_token(
    client: httpx.AsyncClient,
    *,
    token_endpoint: str,
    credentials: ClientCredentials,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> TokenResponse:
    """Redeem an authorization code for an EHR access token.

    A confidential client authenticates with HTTP Basic, which is what SMART on
    FHIR asks for when a secret exists; a client registered without a secret is
    a public client and authenticates with PKCE alone. Both send the
    ``code_verifier``, because PKCE is not the confidential client's fallback —
    SMART on FHIR 2.0 requires it of both.

    Args:
        client: The HTTP client to post with.
        token_endpoint: From the EHR's discovery document.
        credentials: The registered client for this EHR.
        code: The authorization code the callback received.
        redirect_uri: The same value sent on the authorization request.
        code_verifier: The verifier whose challenge went on that request.

    Returns:
        The parsed token response.

    Raises:
        TokenExchangeError: If the request fails, the server refuses, or the
            response carries no access token.
    """
    return await _post_token_request(
        client,
        token_endpoint=token_endpoint,
        credentials=credentials,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": credentials.client_id,
        },
    )


async def refresh_access_token(
    client: httpx.AsyncClient,
    *,
    token_endpoint: str,
    credentials: ClientCredentials,
    refresh_token: str,
) -> TokenResponse:
    """Renew an EHR access token from the refresh token a launch already holds.

    TASK-051b. This is the same conversation with the same authorization server
    as ``exchange_code_for_token``, differing only in the grant presented, so it
    shares that function's client authentication and its response handling
    rather than restating them — a second spelling of how a vendor's client
    authenticates is how the two would come to disagree.

    No PKCE here: ``code_verifier`` proves that whoever redeems a code is who
    requested it, and a refresh redeems no code. The refresh token is itself the
    proof.

    The caller is expected to distinguish ``TokenGrantRejected`` from
    ``TokenEndpointUnavailable``: only the first means the launch is over. See
    CLAUDE.md, "The launch record outlives its access token".

    Args:
        client: The HTTP client to post with.
        token_endpoint: Carried on the launch record, from the same discovery
            document the authorization request used.
        credentials: The registered client for this EHR.
        refresh_token: The value stored at the last exchange or refresh.

    Returns:
        The parsed token response. Its ``refresh_token`` may be a *new* value —
        servers commonly rotate — and the caller must store whichever it gets.

    Raises:
        TokenGrantRejected: The server refused the grant; the launch is over.
        TokenEndpointUnavailable: No usable answer; the grant may still be good.
        TokenExchangeError: The answer was unusable in some other way.
    """
    return await _post_token_request(
        client,
        token_endpoint=token_endpoint,
        credentials=credentials,
        form={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": credentials.client_id,
        },
    )


async def _post_token_request(
    client: httpx.AsyncClient,
    *,
    token_endpoint: str,
    credentials: ClientCredentials,
    form: dict[str, str],
) -> TokenResponse:
    """Post one grant to a token endpoint and parse what comes back.

    Shared by the code exchange and the refresh because the parts that are easy
    to get subtly wrong — confidential-versus-public client authentication, and
    which failures are which — must be identical for both.

    Raises:
        TokenGrantRejected: On a 4xx, which is the server deciding.
        TokenEndpointUnavailable: On a transport failure or a 5xx.
        TokenExchangeError: On an answer that is neither, and is unusable.
    """
    # A public client sends no Authorization header at all, which httpx spells
    # with its USE_CLIENT_DEFAULT sentinel. The sentinel is public; only its
    # class is not, which is why the annotation reaches into httpx._client
    # rather than using Any.
    auth: tuple[str, str] | httpx._client.UseClientDefault = httpx.USE_CLIENT_DEFAULT
    if credentials.client_secret is not None:
        auth = (credentials.client_id, credentials.client_secret.get_secret_value())

    try:
        response = await client.post(
            token_endpoint,
            data=form,
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=TOKEN_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        raise TokenEndpointUnavailable(
            f"request failed ({type(exc).__name__})", timed_out=True
        ) from exc
    except httpx.HTTPError as exc:
        raise TokenEndpointUnavailable(f"request failed ({type(exc).__name__})") from exc

    if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise TokenEndpointUnavailable(f"authorization server answered HTTP {response.status_code}")

    if response.status_code != httpx.codes.OK:
        raise TokenGrantRejected(
            f"authorization server answered HTTP {response.status_code}"
            f"{_oauth_error_suffix(response)}",
            oauth_error=_oauth_error_code(response),
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise TokenExchangeError("response was not JSON") from exc

    try:
        return TokenResponse.model_validate(payload)
    except ValidationError as exc:
        raise TokenExchangeError("response carried no usable access_token") from exc


def _oauth_error_suffix(response: httpx.Response) -> str:
    """Return the OAuth ``error`` code from a failed exchange, if there is one.

    RFC 6749 gives the failure a machine-readable code, and it is the one part
    of the body worth surfacing: ``invalid_grant`` and ``invalid_client`` mean
    very different things to whoever is debugging a launch. Only that code is
    read — never ``error_description``, which is free text an authorization
    server may fill with an echo of the request.
    """
    code = _oauth_error_code(response)
    return f" ({code})" if code else ""


def _oauth_error_code(response: httpx.Response) -> str | None:
    """Return RFC 6749's ``error`` code from a refused token request, if present.

    Split out from the message-building above because a refresh needs the code
    as a value rather than as prose: ``invalid_grant`` is what tells the caller
    the grant is dead rather than merely refused this once. The same rule
    applies — only the code is read, never ``error_description``.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return str(body["error"])
    return None

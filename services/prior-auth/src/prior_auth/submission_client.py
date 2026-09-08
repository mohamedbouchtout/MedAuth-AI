"""Talking to the two services a submission passes through.

TASK-061. The router does two things over HTTP and nothing else: it reads what a
path is chosen from, and — when there is an automated path — it asks
``fhir-integration`` to submit.

**Neither call is made by importing the other service.** The routing read goes
through ``track-a-clinical``'s route because that service owns the row; the
submission goes through ``fhir-integration``'s route because that service holds
the EHR credential and the adapter that speaks Da Vinci PAS. Same arrangement,
and same argument, as this service's chart-context read one task earlier.

**The routing read is deliberately the narrow one.**
``GET /prior-auth/{request_id}/routing`` returns a payer, a launch and a
submittable flag; the full read next door returns note excerpts and writes a
``READ_PRIOR_AUTH`` row. A router looks at none of that, so asking for it would
pull a patient's documentation across the network for a decision that ignores it
and record a clinical access that did not happen.

**This module records nothing.** ``POST /fhir/prior-auth`` already calls back
into ``track-a-clinical`` to record what the payer said, and that route is the
only writer of ``submission_method``, ``payer_outcome`` and
``payer_reference_number``. A second writer here would defeat the write-once
constraint that makes a duplicate submission refusable.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

#: How ``track-a-clinical``'s routing read is addressed.
ROUTING_PATH: Final = "/prior-auth/{request_id}/routing"

#: How ``fhir-integration``'s submission is addressed.
SUBMIT_PATH: Final = "/fhir/prior-auth"

#: The header that route takes its launch in, matching every other ``/fhir/*``
#: route. Not a query parameter: it is a credential handle.
LAUNCH_ID_HEADER: Final = "X-MedAuth-Launch-Id"

#: The submitter's answer when a request may not be sent. Relayed rather than
#: re-derived — that service asked the owning service, which owns the rule.
ERROR_CODE_ALREADY_SUBMITTED: Final = "PRIOR_AUTH_ALREADY_SUBMITTED"

#: The submitter's answer when the payer takes PAS but this EHR delivers through
#: CoverMyMeds and nothing configured it. The same outcome as a payer with no
#: automated path, arriving one layer down — see :mod:`prior_auth.router`.
ERROR_CODE_PATH_NOT_CONFIGURED: Final = "PRIOR_AUTH_PATH_NOT_CONFIGURED"


class RoutingFacts(BaseModel):
    """What a submission path is chosen from.

    Attributes:
        request_id: The request this describes.
        status: Where the request has got to in our own process.
        payer_name: The payer's own display name, **never a slug** — the router
            normalises it before asking about capabilities.
        launch_id: The SMART launch holding the EHR credential, or None when the
            visit was started outside one.
        submittable: Whether the request may be sent now. Computed by the owning
            service, never re-derived here.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    request_id: str
    status: str
    payer_name: str | None = None
    launch_id: str | None = None
    submittable: bool = True


class SubmissionResult(BaseModel):
    """What the submitter reported a payer said.

    Attributes:
        request_id: The request that was submitted.
        outcome: The payer's own answer — ``complete``, ``queued``, ``partial``
            or ``error``. Carried rather than collapsed into "submitted", so a
            queued request and an adjudicated one stay distinguishable.
        submission_method: Which path transmitted it. Reported by the adapter,
            never predicted by the router.
        payer_reference_number: The payer's reference, when it gave one.
            Legitimately absent on a queued answer.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    request_id: str
    outcome: str
    submission_method: str
    payer_reference_number: str | None = None


class RoutingReadError(Exception):
    """The routing facts could not be read.

    Attributes:
        not_found: Whether the request simply does not exist, which is the one
            failure here that retrying cannot fix.
        timed_out: Whether the call timed out, which the route reports as 504.
    """

    def __init__(self, detail: str, *, not_found: bool = False, timed_out: bool = False):
        super().__init__(detail)
        self.detail = detail
        self.not_found = not_found
        self.timed_out = timed_out


class SubmissionUnavailable(Exception):
    """The submitter could not be reached, or failed in a way we may retry.

    Attributes:
        timed_out: Whether the call timed out. **A timeout is not a failed
            submission** — the payer may have taken the request in — so nothing
            here retries automatically, exactly as the submitter itself refuses
            to retry an ambiguous create.
    """

    def __init__(self, detail: str, *, timed_out: bool = False):
        super().__init__(detail)
        self.detail = detail
        self.timed_out = timed_out


class SubmissionRefused(Exception):
    """The submitter refused the request, and said why in a code we relay.

    Attributes:
        code: The submitter's own error code. The router branches on
            :data:`ERROR_CODE_PATH_NOT_CONFIGURED` and
            :data:`ERROR_CODE_ALREADY_SUBMITTED`; anything else is reported as a
            refusal a person has to look at.
        status_code: The status that came back, for logging.
    """

    def __init__(self, code: str, *, status_code: int):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _error_code(response: httpx.Response) -> str:
    """Return the envelope's error code, or an empty string when there is none.

    Never the message: that service's messages can name a payer reference and
    this one's log lines should carry identifiers rather than prose it did not
    write.
    """
    try:
        error = response.json().get("error") or {}
        code = error.get("code")
    except ValueError:
        return ""
    return str(code) if code else ""


async def read_routing_facts(
    request_id: str,
    *,
    base_url: str,
    timeout_seconds: float,
    client: httpx.AsyncClient | None = None,
) -> RoutingFacts:
    """Read what a submission path is chosen from.

    Args:
        request_id: The prior-authorization request being routed.
        base_url: ``track-a-clinical``'s base URL.
        timeout_seconds: Per-request timeout.
        client: An HTTP client to use, for tests. One is opened per call
            otherwise, matching this service's existing chart-context read.

    Returns:
        The routing facts.

    Raises:
        RoutingReadError: The request does not exist, or the read failed.
    """
    url = base_url.rstrip("/") + ROUTING_PATH.format(request_id=request_id)

    try:
        async with _client(client, timeout_seconds) as http:
            response = await http.get(url)
            if response.status_code == httpx.codes.NOT_FOUND:
                raise RoutingReadError("no such prior-authorization request", not_found=True)
            response.raise_for_status()
            payload = response.json()
        return RoutingFacts.model_validate(payload["data"])
    except httpx.TimeoutException as exc:
        raise RoutingReadError(
            "the clinical service did not answer in time", timed_out=True
        ) from exc
    except (httpx.HTTPError, ValidationError, KeyError, TypeError, ValueError) as exc:
        raise RoutingReadError("the clinical service could not be reached") from exc


async def submit_through_fhir_integration(
    request_id: str,
    *,
    launch_id: str,
    base_url: str,
    timeout_seconds: float,
    client: httpx.AsyncClient | None = None,
) -> SubmissionResult:
    """Ask ``fhir-integration`` to submit this request to the payer.

    The body is the request id and nothing else, exactly as that route requires:
    everything the submission needs is resolved server-side, and a caller posting
    the bundle back would put a payer submission's payload under the control of
    the least trusted participant in it.

    Args:
        request_id: The request to submit.
        launch_id: The SMART launch whose EHR credential the submission is made
            with. A credential handle — never logged, and never in the URL.
        base_url: ``fhir-integration``'s base URL.
        timeout_seconds: Per-request timeout.
        client: An HTTP client to use, for tests.

    Returns:
        What the payer said, and which path transmitted it.

    Raises:
        SubmissionRefused: The submitter refused, with a code to branch on.
        SubmissionUnavailable: The submitter or the payer could not be reached.
    """
    url = base_url.rstrip("/") + SUBMIT_PATH

    try:
        async with _client(client, timeout_seconds) as http:
            response = await http.post(
                url,
                json={"request_id": request_id},
                headers={LAUNCH_ID_HEADER: launch_id},
            )
            if response.is_client_error:
                raise SubmissionRefused(
                    _error_code(response),
                    status_code=response.status_code,
                )
            response.raise_for_status()
            payload = response.json()
        return SubmissionResult.model_validate(payload["data"])
    except httpx.TimeoutException as exc:
        # Deliberately not retried. The payer may have taken the request in, and
        # a second submission asks it to open a second review — the failure this
        # whole path is arranged to prevent.
        raise SubmissionUnavailable(
            "the submission did not complete in time", timed_out=True
        ) from exc
    except (httpx.HTTPError, ValidationError, KeyError, TypeError, ValueError) as exc:
        raise SubmissionUnavailable("the submission service could not be reached") from exc


def _client(client: httpx.AsyncClient | None, timeout_seconds: float) -> Any:
    """Return the injected client, or a new one, as an async context manager.

    An injected client is not closed here — it belongs to whoever passed it.
    """
    if client is not None:
        return _Borrowed(client)
    return httpx.AsyncClient(timeout=timeout_seconds)


class _Borrowed:
    """Uses a client without owning it, so a test's client survives the call."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *_exc: object) -> None:
        return None

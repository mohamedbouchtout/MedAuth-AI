"""Resolving a practitioner into a ``provider_id`` (TASK-025b).

The registry lives in ``track-a-clinical``; this is the client that asks it.
What is asserted here is mostly failure handling, because the caller
(``GET /fhir/launch-context``) turns every failure into a null ``provider_id``
rather than a failed launch, and a client that raised something else would break
that. The other half is that a practitioner reference never reaches a log line
or an error message.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from src.providers_client import RESOLVE_PATH, ProvidersClient, ProviderServiceError

BASE_URL = "http://track-a-clinical:8003"
PRACTITIONER_REF = "https://ehr.example-hospital.org/r4/Practitioner/prov-77"
PROVIDER_ID = "8f14e45f-ceea-467a-9c0e-1b2a3c4d5e6f"
TIMEOUT = 5.0


def client_over(handler: Any) -> ProvidersClient:
    """A client whose one call is answered by ``handler``."""
    return ProvidersClient(
        BASE_URL, httpx.AsyncClient(transport=httpx.MockTransport(handler)), TIMEOUT
    )


def answering(body: Any, status_code: int = 200) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return handler


@pytest.mark.asyncio
async def test_it_posts_the_reference_and_returns_the_provider_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"provider_id": PROVIDER_ID}, "error": None})

    assert await client_over(handler).resolve(PRACTITIONER_REF) == PROVIDER_ID

    assert seen[0].method == "POST"
    assert seen[0].url.path == RESOLVE_PATH
    # The absolute reference, sent whole: a bare id would merge two people on
    # two servers into one provider.
    assert json.loads(seen[0].read()) == {"fhir_practitioner_ref": PRACTITIONER_REF}


@pytest.mark.asyncio
async def test_an_unconfigured_registry_fails_before_any_call() -> None:
    """An empty base URL is a configuration gap, said plainly.

    The same arrangement as ``track_a_clinical_url`` elsewhere in this service:
    an env var that is present and unread is a worse trap than an absent one, so
    the failure names the configuration rather than happening inside an HTTP call
    to an empty host.
    """
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200)

    unconfigured = ProvidersClient(
        "", httpx.AsyncClient(transport=httpx.MockTransport(handler)), TIMEOUT
    )

    with pytest.raises(ProviderServiceError, match="not configured"):
        await unconfigured.resolve(PRACTITIONER_REF)
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (httpx.TimeoutException("slow"), "did not answer in time"),
        (httpx.ConnectError("refused"), "could not be reached"),
    ],
    ids=["timeout", "transport"],
)
async def test_a_transport_failure_is_a_provider_service_error(
    outcome: Exception, expected: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise outcome

    with pytest.raises(ProviderServiceError, match=expected):
        await client_over(handler).resolve(PRACTITIONER_REF)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 404, 422, 500, 503])
async def test_a_refusal_is_a_provider_service_error(status_code: int) -> None:
    """Every non-2xx, without the caller having to read a status.

    The route above turns all of them into the same null ``provider_id``, so
    there is nothing here for a distinction to buy — unlike the note client,
    whose 404 genuinely means something different from its 502.
    """
    with pytest.raises(ProviderServiceError):
        await client_over(answering({}, status_code)).resolve(PRACTITIONER_REF)


@pytest.mark.asyncio
async def test_a_non_json_answer_is_a_provider_service_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    with pytest.raises(ProviderServiceError, match="not JSON"):
        await client_over(handler).resolve(PRACTITIONER_REF)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": None}, "carried no data"),
        ({"data": None, "error": {"code": "x"}}, "carried no data"),
        ({"data": {}, "error": None}, "was not usable"),
        ({"data": {"provider": PROVIDER_ID}, "error": None}, "was not usable"),
    ],
    ids=["no-data", "error-envelope", "empty-data", "wrong-field"],
)
async def test_an_unusable_payload_is_a_provider_service_error(
    body: dict[str, Any], expected: str
) -> None:
    """A renamed field is a failure, never a silently absent provider.

    Returning ``None`` here instead would be indistinguishable from a launch
    whose actor was never verified, and the two call for different responses:
    one is a deployment mismatch to fix, the other is normal.
    """
    with pytest.raises(ProviderServiceError, match=expected):
        await client_over(answering(body)).resolve(PRACTITIONER_REF)


@pytest.mark.asyncio
async def test_no_failure_puts_the_reference_in_its_message_or_a_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reference identifies an individual clinician.

    httpx puts the request URL in its own exception messages, which is why the
    client never renders one — the same rule the adapter layer follows for a
    search URL carrying a patient id.
    """
    with caplog.at_level(logging.DEBUG):
        for handler in (
            answering({}, 500),
            answering({"data": {}, "error": None}),
        ):
            with pytest.raises(ProviderServiceError) as caught:
                await client_over(handler).resolve(PRACTITIONER_REF)
            assert PRACTITIONER_REF not in str(caught.value)

    ours = [record for record in caplog.records if record.name.startswith("src.")]
    assert ours, "expected the refusal to be logged"
    assert all(PRACTITIONER_REF not in record.getMessage() for record in ours)
    assert all("prov-77" not in record.getMessage() for record in ours)

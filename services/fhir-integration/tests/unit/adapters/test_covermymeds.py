"""The CoverMyMeds submission path's failure behaviour (TASK-054).

The happy path is exercised end to end through the route; what is worth pinning
here is what this module does when the vendor answers something unexpected —
because the field mapping is explicitly unverified, and the value of writing it
down is lost if an unknown answer is quietly given a default.

Nothing here asserts that the *mapping* is right. It cannot: there is no sandbox
and no published schema, which is what ``covermymeds.py`` says at length.
"""

from __future__ import annotations

import httpx
import pytest

from src.adapters.covermymeds import (
    CoverMyMedsClient,
    CoverMyMedsNotConfigured,
)
from src.adapters.errors import (
    FHIRAuthorizationExpired,
    FHIRMalformedResponse,
    FHIRUpstreamUnavailable,
)
from src.adapters.models import (
    NoteCode,
    PriorAuthContent,
    PriorAuthEvidence,
    PriorAuthProcedure,
    SubmissionMethod,
    SubmissionOutcome,
)

API_KEY = "covermymeds-secret-key"
BASE_URL = "https://covermymeds.test"


def content() -> PriorAuthContent:
    return PriorAuthContent(
        request_id="request-1",
        patient_id="patient-7",
        encounter_id="encounter-4",
        provider_reference="https://ehr.example.org/fhir/Practitioner/pr-1",
        payer_name="Aetna",
        procedures=[PriorAuthProcedure(cpt_code="27447", description="knee replacement")],
        icd10_codes=[
            NoteCode(code="M17.11", source="llm-extraction"),
            NoteCode(code="E11.9", source="comprehend-medical"),
        ],
        clinical_evidence=[PriorAuthEvidence(text="12 weeks of physical therapy")],
    )


def client(
    handler: object, *, base_url: str = BASE_URL, api_key: str = API_KEY
) -> CoverMyMedsClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return CoverMyMedsClient(base_url, api_key, httpx.AsyncClient(transport=transport))


def answer(payload: dict[str, object], status: int = 200) -> object:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


async def test_a_submission_reports_the_vendors_reference() -> None:
    result = await client(answer({"status": "approved", "reference_number": "CMM-1"})).submit(
        content()
    )

    assert result.outcome is SubmissionOutcome.COMPLETE
    assert result.payer_reference_number == "CMM-1"
    assert result.submission_method is SubmissionMethod.COVERMYMEDS


async def test_a_pending_answer_is_queued_rather_than_complete() -> None:
    """The two are different facts, and the caller records which."""
    result = await client(answer({"status": "pending"})).submit(content())

    assert result.outcome is SubmissionOutcome.QUEUED
    assert result.payer_reference_number is None


async def test_an_unknown_status_is_refused_rather_than_defaulted() -> None:
    """The mapping is unverified, so the unknown case is exactly the one to not guess.

    An outcome recorded wrongly is indistinguishable afterwards from one the
    payer actually gave — which is why this raises instead of falling back.
    """
    with pytest.raises(FHIRMalformedResponse):
        await client(answer({"status": "under_review"})).submit(content())


async def test_a_missing_status_is_refused_too() -> None:
    with pytest.raises(FHIRMalformedResponse):
        await client(answer({"reference_number": "CMM-1"})).submit(content())


async def test_a_rejected_credential_is_reported_as_such() -> None:
    with pytest.raises(FHIRAuthorizationExpired):
        await client(answer({}, status=401)).submit(content())


async def test_a_vendor_outage_is_transient() -> None:
    with pytest.raises(FHIRUpstreamUnavailable):
        await client(answer({}, status=503)).submit(content())


async def test_a_refused_request_is_malformed_rather_than_transient() -> None:
    """Retrying an identical request would only be refused again."""
    with pytest.raises(FHIRMalformedResponse):
        await client(answer({}, status=400)).submit(content())


async def test_a_timeout_is_never_silently_retried() -> None:
    """It is genuinely ambiguous: the vendor may have taken the request."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow")

    with pytest.raises(FHIRUpstreamUnavailable) as raised:
        await client(handler).submit(content())

    assert raised.value.timed_out


async def test_a_non_json_answer_is_malformed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(FHIRMalformedResponse):
        await client(handler).submit(content())


async def test_a_non_object_answer_is_malformed() -> None:
    with pytest.raises(FHIRMalformedResponse):
        await client(answer(["a list"])).submit(content())  # type: ignore[arg-type]


async def test_an_unconfigured_path_says_so_before_any_request() -> None:
    """Rather than failing inside an HTTP call to an empty host."""
    with pytest.raises(CoverMyMedsNotConfigured):
        await client(answer({"status": "approved"}), base_url="").submit(content())


async def test_a_missing_api_key_is_unconfigured_too() -> None:
    with pytest.raises(CoverMyMedsNotConfigured):
        await client(answer({"status": "approved"}), api_key="").submit(content())


def test_a_machine_suggested_code_is_not_in_the_request_body() -> None:
    """The filter is a property of leaving this system, not of speaking FHIR."""
    body = client(answer({})).build_request(content())

    assert [code["icd10_code"] for code in body["diagnoses"]] == ["M17.11"]


def test_the_client_never_renders_its_api_key() -> None:
    """It reaches log lines and tracebacks that a credential must not."""
    rendered = repr(client(answer({})))

    assert API_KEY not in rendered
    assert BASE_URL in rendered

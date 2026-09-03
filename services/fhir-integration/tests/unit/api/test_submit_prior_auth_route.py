"""``POST /fhir/prior-auth`` — submitting an authorization request. TASK-054.

The same shape as the note write-back's tests one task earlier, and what is
asserted beyond the happy path is again mostly about refusing to do harm: that a
request already submitted is not submitted twice, that a request missing
something a payer needs is refused rather than sent half-built, and that the one
failure where the payer *did* take the request reports itself as such instead of
inviting a retry that opens a second review.

Two things here are the profile's rules rather than ours, and both are asserted
**on the bytes that went on the wire** rather than on the builder's return value
— a builder that stopped being called would otherwise still pass.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from hipaa_logger import AuditAction
from src.adapters.factory import EHRType
from src.api import fhir as fhir_routes
from src.api.dependencies import get_http_client, get_redis
from src.config import get_settings
from src.main import create_app
from src.smart import store
from src.smart.store import LaunchToken
from tests.unit.api.conftest import TOKEN_ENDPOINT, FakeRedis
from tests.unit.api.test_fhir_routes import PRACTITIONER_REF, AuditRecorder
from tests.unit.conftest import ACCESS_TOKEN, FHIR_BASE_URL

LAUNCH_ID = "3f2a7c18-0d64-4a51-9f0e-8b1c2d3e4f50"
HEADERS = {fhir_routes.LAUNCH_ID_HEADER: LAUNCH_ID}
REQUEST_ID = str(uuid.uuid4())
SESSION_ID = str(uuid.uuid4())

TRACK_A_URL = "http://track-a-clinical.test"
COVERMYMEDS_URL = "https://covermymeds.test"
PAYER_REFERENCE = "AUTH-88213"

LLM_CODE = {
    "code": "M17.11",
    "display": "Unilateral primary osteoarthritis, right knee",
    "source": "llm-extraction",
}
SUGGESTED_CODE = {
    "code": "E11.9",
    "display": "Type 2 diabetes mellitus without complications",
    "source": "comprehend-medical",
}


def claim_response_bundle(
    *, outcome: str = "complete", pre_auth_ref: str | None = PAYER_REFERENCE
) -> dict[str, Any]:
    """A PAS response bundle, including an entry type this package does not model."""
    response: dict[str, Any] = {
        "resourceType": "ClaimResponse",
        "status": "active",
        "type": {"text": "professional"},
        "use": "preauthorization",
        "patient": {"reference": "Patient/patient-7"},
        "created": "2026-09-03T10:04:09Z",
        "insurer": {"display": "Aetna"},
        "outcome": outcome,
    }
    if pre_auth_ref is not None:
        response["preAuthRef"] = pre_auth_ref
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"fullUrl": "urn:uuid:claimresponse-1", "resource": response},
            # Conformant in a PAS response and unmodelled here — the entry the
            # UnknownResource fallback exists for.
            {
                "fullUrl": "urn:uuid:practitioner-1",
                "resource": {"resourceType": "Practitioner", "id": "pr-1"},
            },
        ],
    }


class FakeUpstreams:
    """The clinical service, the payer's FHIR endpoint and CoverMyMeds, behind one transport.

    One transport because the route uses one pooled HTTP client for all of them,
    which is itself worth pinning: separate clients per upstream would not notice
    code that constructed its own.
    """

    def __init__(self) -> None:
        self.request: dict[str, Any] = {
            "request_id": REQUEST_ID,
            "session_id": SESSION_ID,
            "patient_fhir_id": "patient-7",
            "ehr_encounter_id": "encounter-4",
            "payer_name": "Aetna",
            "insurance_plan_type": "PPO",
            "insurance_member_id": "W123456789",
            "procedures": [{"cpt_code": "27447", "description": "total knee replacement"}],
            "diagnoses": [LLM_CODE, SUGGESTED_CODE],
            "clinical_evidence": [{"text": "12 weeks of physical therapy, no improvement"}],
            "submitted_at": None,
            "payer_reference_number": None,
        }
        self.pas_response: dict[str, Any] = claim_response_bundle()
        self.covermymeds_response: dict[str, Any] = {
            "status": "pending",
            "reference_number": PAYER_REFERENCE,
        }
        #: Per-path failures, as ``{path fragment: response or exception}``.
        self.failures: dict[str, httpx.Response | Exception] = {}
        self.submitted: list[dict[str, Any]] = []
        self.covermymeds_submitted: list[dict[str, Any]] = []
        self.recorded: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []

    def fail(self, fragment: str, outcome: httpx.Response | Exception) -> None:
        self.failures[fragment] = outcome

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, str(request.url)))

        for fragment, outcome in self.failures.items():
            if fragment in str(request.url):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        if request.url.host == httpx.URL(TRACK_A_URL).host:
            if request.method == "PATCH":
                self.recorded.append(json.loads(request.content))
            return httpx.Response(200, json={"data": self.request, "error": None})

        if request.url.host == httpx.URL(COVERMYMEDS_URL).host:
            self.covermymeds_submitted.append(json.loads(request.content))
            return httpx.Response(200, json=self.covermymeds_response)

        if request.method == "POST" and path.endswith("/Claim/$submit"):
            self.submitted.append(json.loads(request.content))
            return httpx.Response(200, json=self.pas_response)

        return httpx.Response(404, json={"resourceType": "OperationOutcome"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def sent_claim(self) -> dict[str, Any]:
        """The Claim as it actually went on the wire."""
        return dict(self.submitted[0]["entry"][0]["resource"])


@pytest.fixture
def upstreams() -> FakeUpstreams:
    return FakeUpstreams()


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> AuditRecorder:
    recorder = AuditRecorder()
    monkeypatch.setattr("src.audit.audit_log", recorder)
    return recorder


def build_client(
    upstreams: FakeUpstreams,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ehr_type: EHRType = EHRType.GENERIC,
    covermymeds_url: str = "",
) -> Iterator[TestClient]:
    get_settings.cache_clear()
    monkeypatch.setenv("TRACK_A_CLINICAL_URL", TRACK_A_URL)
    monkeypatch.setenv("COVERMYMEDS_BASE_URL", covermymeds_url)
    monkeypatch.setenv("COVERMYMEDS_API_KEY", "covermymeds-key" if covermymeds_url else "")

    redis = FakeRedis()
    redis.values[store.token_key(LAUNCH_ID)] = LaunchToken(
        ehr_type=ehr_type,
        fhir_base_url=FHIR_BASE_URL,
        access_token=ACCESS_TOKEN,
        access_token_expires_at=store.access_token_expiry(3600),
        token_endpoint=TOKEN_ENDPOINT,
        refresh_token="ehr-refresh-token",
        patient_id="patient-7",
        encounter_id="encounter-4",
        fhir_practitioner_ref=PRACTITIONER_REF,
    ).model_dump_json()

    app = create_app()
    app.dependency_overrides[get_redis] = lambda: redis
    http = upstreams.client()
    app.dependency_overrides[get_http_client] = lambda: http
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture
def client(
    upstreams: FakeUpstreams, monkeypatch: pytest.MonkeyPatch, audit: AuditRecorder
) -> Iterator[TestClient]:
    """A client whose launch is live and whose upstreams are fakes."""
    yield from build_client(upstreams, monkeypatch)


@pytest.fixture
def athena_client(
    upstreams: FakeUpstreams, monkeypatch: pytest.MonkeyPatch, audit: AuditRecorder
) -> Iterator[TestClient]:
    """The same, launched from Athenahealth with CoverMyMeds configured."""
    yield from build_client(
        upstreams, monkeypatch, ehr_type=EHRType.ATHENA, covermymeds_url=COVERMYMEDS_URL
    )


def submit(client: TestClient, **body: Any) -> httpx.Response:
    return client.post("/fhir/prior-auth", json={"request_id": REQUEST_ID} | body, headers=HEADERS)


def test_the_request_is_submitted_and_the_answer_returned(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    response = submit(client)

    assert response.status_code == 201
    assert response.json()["data"] == {
        "request_id": REQUEST_ID,
        "outcome": "complete",
        "payer_reference_number": PAYER_REFERENCE,
        "submission_method": "fhir-pas",
    }
    assert len(upstreams.submitted) == 1


def test_the_bundle_on_the_wire_satisfies_the_request_profile(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """``profile-pas-request-bundle``: collection, identifier, timestamp, ClaimFirst.

    Asserted on what was actually posted rather than on the builder's return
    value, so a route that stopped calling the builder would fail here.
    """
    submit(client)
    bundle = upstreams.submitted[0]

    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "collection"
    assert bundle["identifier"]["value"]
    assert bundle["timestamp"]
    assert bundle["entry"][0]["resource"]["resourceType"] == "Claim"


def test_the_bundle_carries_no_prohibited_entry_fields(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """The profile prohibits ``search``/``request``/``response`` on an entry."""
    submit(client)

    for entry in upstreams.submitted[0]["entry"]:
        assert not {"search", "request", "response"} & set(entry)


def test_the_claim_asks_for_authorization_rather_than_payment(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    assert submit(client).status_code == 201
    assert upstreams.sent_claim()["use"] == "preauthorization"


def test_a_machine_suggested_diagnosis_never_reaches_the_payer(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """A bundle asserts what the provider documented, not what a model proposed.

    CLAUDE.md, "Writing clinical data out to the EHR". The filter lives in the
    builder, and this asserts it on the wire.
    """
    submit(client)
    codes = {
        coding["code"]
        for diagnosis in upstreams.sent_claim()["diagnosis"]
        for coding in diagnosis["diagnosisCodeableConcept"]["coding"]
    }

    assert codes == {"M17.11"}
    assert SUGGESTED_CODE["code"] not in json.dumps(upstreams.submitted[0])


def test_the_coverage_travels_in_the_bundle_and_the_claim_points_at_it(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """The Coverage gap is closed by carrying the resource, never by inventing an id."""
    submit(client)
    bundle = upstreams.submitted[0]
    coverage_entry = bundle["entry"][1]

    assert coverage_entry["resource"]["resourceType"] == "Coverage"
    assert coverage_entry["fullUrl"].startswith("urn:uuid:")
    assert (
        upstreams.sent_claim()["insurance"][0]["coverage"]["reference"]
        == (coverage_entry["fullUrl"])
    )
    assert coverage_entry["resource"]["subscriberId"] == "W123456789"


def test_the_provider_is_the_launchs_verified_practitioner(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """Never ``encounters.provider_id``, a UUID that identifies nobody to a payer."""
    submit(client)

    assert upstreams.sent_claim()["provider"]["reference"] == PRACTITIONER_REF


def test_the_result_is_recorded_against_the_request(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    submit(client)

    assert upstreams.recorded == [
        {
            "submission_method": "fhir-pas",
            "outcome": "complete",
            "payer_reference_number": PAYER_REFERENCE,
        }
    ]


def test_a_queued_answer_with_no_reference_is_recorded_as_queued(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """``preAuthRef`` is 0..1 and absent on a queued answer — not a failure.

    The row must not end up saying the request was completed, which is what a
    result carrying only a reference number would have allowed.
    """
    upstreams.pas_response = claim_response_bundle(outcome="queued", pre_auth_ref=None)

    response = submit(client)

    assert response.status_code == 201
    assert response.json()["data"]["outcome"] == "queued"
    assert response.json()["data"]["payer_reference_number"] is None
    assert upstreams.recorded[0]["outcome"] == "queued"
    assert upstreams.recorded[0]["payer_reference_number"] is None


def test_a_payer_refusal_is_carried_rather_than_dropped(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """``error`` is a conformant answer, and recording it as submitted would lie."""
    upstreams.pas_response = claim_response_bundle(outcome="error", pre_auth_ref=None)

    response = submit(client)

    assert response.status_code == 201
    assert response.json()["data"]["outcome"] == "error"
    assert upstreams.recorded[0]["outcome"] == "error"


def test_the_submission_is_audited(
    client: TestClient, upstreams: FakeUpstreams, audit: AuditRecorder
) -> None:
    """A disclosure of PHI to a third party, under its own action."""
    submit(client)

    assert [call["action"] for call in audit.calls] == [AuditAction.SUBMIT_PRIOR_AUTH]
    assert audit.calls[0]["resource_type"] == "PriorAuthRequest"
    assert audit.calls[0]["resource_id"] == REQUEST_ID
    assert audit.calls[0]["session_id"] == SESSION_ID
    assert audit.calls[0]["fhir_practitioner_ref"] == PRACTITIONER_REF


def test_a_request_already_submitted_is_refused_before_the_payer_is_called(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    upstreams.request["submitted_at"] = "2026-09-03T09:00:00Z"

    response = submit(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PRIOR_AUTH_ALREADY_SUBMITTED"
    assert upstreams.submitted == []


def test_a_request_with_no_procedure_is_refused(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """Nothing to seek authorization for, and nothing is sent."""
    upstreams.request["procedures"] = []

    response = submit(client)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PRIOR_AUTH_NOT_SUBMITTABLE"
    assert upstreams.submitted == []


def test_a_launch_with_no_verified_provider_is_refused(
    upstreams: FakeUpstreams, monkeypatch: pytest.MonkeyPatch, audit: AuditRecorder
) -> None:
    """``Claim.provider`` is 1..1, and a provider we cannot name is not invented."""
    get_settings.cache_clear()
    monkeypatch.setenv("TRACK_A_CLINICAL_URL", TRACK_A_URL)
    redis = FakeRedis()
    redis.values[store.token_key(LAUNCH_ID)] = LaunchToken(
        ehr_type=EHRType.GENERIC,
        fhir_base_url=FHIR_BASE_URL,
        access_token=ACCESS_TOKEN,
        access_token_expires_at=store.access_token_expiry(3600),
        token_endpoint=TOKEN_ENDPOINT,
        patient_id="patient-7",
        fhir_practitioner_ref=None,
    ).model_dump_json()
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: redis
    http = upstreams.client()
    app.dependency_overrides[get_http_client] = lambda: http

    with TestClient(app) as test_client:
        response = submit(test_client)

    get_settings.cache_clear()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PRIOR_AUTH_NOT_SUBMITTABLE"
    assert upstreams.submitted == []


def test_an_unknown_request_is_a_404(client: TestClient, upstreams: FakeUpstreams) -> None:
    upstreams.fail("/prior-auth/", httpx.Response(404, json={"data": None, "error": {}}))

    response = submit(client)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PRIOR_AUTH_NOT_FOUND"


def test_an_unreachable_clinical_service_submits_nothing(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    upstreams.fail("/prior-auth/", httpx.ConnectError("no route"))

    response = submit(client)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "PRIOR_AUTH_SERVICE_UNAVAILABLE"
    assert upstreams.submitted == []


def test_a_failure_to_record_names_the_reference_and_forbids_a_retry(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """The payer has the request. A plain failure here would invite a second one."""
    upstreams.fail("/submission", httpx.Response(500, json={"data": None, "error": {}}))

    response = submit(client)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "PRIOR_AUTH_RECORD_FAILED"
    assert PAYER_REFERENCE in response.json()["error"]["message"]
    assert "do not retry" in response.json()["error"]["message"]


def test_the_submission_is_audited_even_when_recording_fails(
    client: TestClient, upstreams: FakeUpstreams, audit: AuditRecorder
) -> None:
    """The row exists for exactly the case someone later goes looking for."""
    upstreams.fail("/submission", httpx.Response(500, json={"data": None, "error": {}}))

    submit(client)

    assert [call["action"] for call in audit.calls] == [AuditAction.SUBMIT_PRIOR_AUTH]


def test_a_response_with_no_claim_response_is_not_recorded(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """An OperationOutcome-only answer is no determination, not a silent success."""
    upstreams.pas_response = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": {"resourceType": "OperationOutcome", "issue": []}}],
    }

    response = submit(client)

    assert response.status_code == 422
    assert upstreams.recorded == []


def test_athena_submits_through_covermymeds_instead_of_fhir_pas(
    athena_client: TestClient, upstreams: FakeUpstreams
) -> None:
    """The whole reason the submission is a method on the adapter.

    Athenahealth has no FHIR PAS support, so the base path would post to an
    endpoint that answers 404. The route asks the adapter to submit and gets the
    right path without knowing which EHR answered.
    """
    response = submit(athena_client)

    assert response.status_code == 201
    assert response.json()["data"]["submission_method"] == "covermymeds"
    assert upstreams.submitted == []
    assert len(upstreams.covermymeds_submitted) == 1


def test_the_covermymeds_path_maps_its_status_onto_the_shared_vocabulary(
    athena_client: TestClient, upstreams: FakeUpstreams
) -> None:
    """``pending`` is a queued request, in the same four-way answer PAS uses."""
    response = submit(athena_client)

    assert response.json()["data"]["outcome"] == "queued"
    assert upstreams.recorded[0]["submission_method"] == "covermymeds"


def test_the_covermymeds_path_filters_codes_too(
    athena_client: TestClient, upstreams: FakeUpstreams
) -> None:
    """The filter is not a property of FHIR — it is a property of leaving here."""
    submit(athena_client)
    sent = upstreams.covermymeds_submitted[0]

    assert [code["icd10_code"] for code in sent["diagnoses"]] == ["M17.11"]


def test_an_unrecognised_covermymeds_status_is_never_guessed_at(
    athena_client: TestClient, upstreams: FakeUpstreams
) -> None:
    """The mapping is unverified, so the unknown case refuses rather than defaults."""
    upstreams.covermymeds_response = {"status": "in_review"}

    response = submit(athena_client)

    assert response.status_code == 502
    assert upstreams.recorded == []


def test_an_unconfigured_covermymeds_path_says_so(
    upstreams: FakeUpstreams, monkeypatch: pytest.MonkeyPatch, audit: AuditRecorder
) -> None:
    """Rather than failing inside an HTTP call to an empty host."""
    for test_client in build_client(upstreams, monkeypatch, ehr_type=EHRType.ATHENA):
        response = submit(test_client)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PRIOR_AUTH_PATH_NOT_CONFIGURED"
    assert upstreams.covermymeds_submitted == []

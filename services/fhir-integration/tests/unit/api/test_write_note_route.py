"""``POST /fhir/notes`` — filing a note to a chart. TASK-053.

What is asserted here beyond the happy path is mostly about refusing to do harm:
that a note already filed is not filed twice, that an encounter with no chart
entry is refused rather than guessed at, and that the one failure where the
document *was* created reports itself as such instead of inviting a retry that
would put a second copy on a patient's chart.

The two upstreams are both fakes behind one transport, because the route uses one
pooled HTTP client for both — which is itself worth pinning: a test that gave
them separate clients would not notice code that constructed its own.
"""

from __future__ import annotations

import base64
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
SESSION_ID = str(uuid.uuid4())

TRACK_A_URL = "http://track-a-clinical.test"
DOCUMENT_ID = "docref-9"

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


class FakeUpstreams:
    """The note service and the EHR, behind one transport.

    Both are answered by ``handler`` and told apart by host, which is what the
    route's single pooled client actually sees.
    """

    def __init__(self) -> None:
        self.note: dict[str, Any] = {
            "soap_subjective": "Right knee pain for three months.",
            "soap_objective": "Tenderness over the medial joint line.",
            "soap_assessment": "Likely primary osteoarthritis of the right knee.",
            "soap_plan": "Order MRI right knee.",
            "icd10_codes": [LLM_CODE, SUGGESTED_CODE],
            "cpt_codes": [],
            "reviewed_by_provider": False,
        }
        self.reference: dict[str, Any] = {
            "session_id": SESSION_ID,
            "ehr_encounter_id": "encounter-4",
            "patient_fhir_id": "patient-7",
            "ehr_document_ref_id": None,
        }
        #: Per-path failures, as ``{path fragment: response or exception}``.
        self.failures: dict[str, httpx.Response | Exception] = {}
        self.created: list[dict[str, Any]] = []
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
            return self._note_service(request, path)

        if request.method == "POST" and path.endswith("/DocumentReference"):
            self.created.append(json.loads(request.content))
            return httpx.Response(
                201,
                headers={"Location": f"{FHIR_BASE_URL}/DocumentReference/{DOCUMENT_ID}"},
            )
        return httpx.Response(404, json={"resourceType": "OperationOutcome"})

    def _note_service(self, request: httpx.Request, path: str) -> httpx.Response:
        if path.endswith("/ehr-reference"):
            if request.method == "PATCH":
                self.recorded.append(json.loads(request.content))
                return httpx.Response(200, json={"data": self.reference, "error": None})
            return httpx.Response(200, json={"data": self.reference, "error": None})
        return httpx.Response(200, json={"data": self.note, "error": None})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def written_text(self) -> str:
        """The decoded body of the document that was filed."""
        return base64.b64decode(self.created[0]["content"][0]["attachment"]["data"]).decode()


@pytest.fixture
def upstreams() -> FakeUpstreams:
    return FakeUpstreams()


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> AuditRecorder:
    recorder = AuditRecorder()
    monkeypatch.setattr("src.audit.audit_log", recorder)
    return recorder


@pytest.fixture
def client(
    upstreams: FakeUpstreams, monkeypatch: pytest.MonkeyPatch, audit: AuditRecorder
) -> Iterator[TestClient]:
    """A client whose launch is live and whose two upstreams are fakes."""
    get_settings.cache_clear()
    monkeypatch.setenv("TRACK_A_CLINICAL_URL", TRACK_A_URL)

    redis = FakeRedis()
    redis.values[store.token_key(LAUNCH_ID)] = LaunchToken(
        ehr_type=EHRType.GENERIC,
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


def write(client: TestClient, **body: Any) -> httpx.Response:
    return client.post("/fhir/notes", json={"session_id": SESSION_ID} | body, headers=HEADERS)


def test_the_note_is_filed_and_its_id_returned(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    response = write(client)

    assert response.status_code == 201
    assert response.json()["data"] == {
        "session_id": SESSION_ID,
        "ehr_document_ref_id": DOCUMENT_ID,
    }
    assert upstreams.created[0]["resourceType"] == "DocumentReference"


def test_the_document_id_is_recorded_against_the_note(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """The local record is the second half of the write, and it is a real call."""
    write(client)

    assert upstreams.recorded == [{"ehr_document_ref_id": DOCUMENT_ID}]


def test_the_ehr_is_written_before_the_id_is_recorded(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """Order matters: an orphaned chart document is findable, a false local record is not."""
    write(client)

    methods_and_urls = [url for method, url in upstreams.requests if method in {"POST", "PATCH"}]
    assert methods_and_urls[0].startswith(FHIR_BASE_URL)
    assert methods_and_urls[1].startswith(TRACK_A_URL)


def test_a_machine_suggestion_does_not_reach_the_chart(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """Asserted at the route as well as at the builder: this is the rule, end to end."""
    write(client)

    text = upstreams.written_text()
    assert LLM_CODE["code"] in text
    assert SUGGESTED_CODE["code"] not in text


def test_filing_audits_the_disclosure(client: TestClient, audit: AuditRecorder) -> None:
    """Its own action, carrying the session — unlike a launch-time read, which has none."""
    write(client)

    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["action"] == AuditAction.WRITE_NOTE_TO_EHR
    assert call["resource_type"] == "DocumentReference"
    assert call["resource_id"] == DOCUMENT_ID
    assert call["session_id"] == SESSION_ID
    assert call["actor_id"] is None
    assert call["fhir_practitioner_ref"] == PRACTITIONER_REF


def test_a_note_already_filed_is_refused_without_calling_the_ehr(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """Duplicate clinical documentation is the harm; not calling the EHR is the proof."""
    upstreams.reference["ehr_document_ref_id"] = "docref-already-there"

    response = write(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_NOTE_ALREADY_WRITTEN
    assert upstreams.created == []


def test_an_encounter_with_no_chart_entry_is_refused(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """A visit started outside a SMART launch has nothing to file against."""
    upstreams.reference["ehr_encounter_id"] = None

    response = write(client)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_ENCOUNTER_NOT_LINKED
    assert upstreams.created == []


def test_an_unknown_session_is_a_404(client: TestClient, upstreams: FakeUpstreams) -> None:
    upstreams.fail("/ehr-reference", httpx.Response(404, json={"data": None, "error": {}}))

    response = write(client)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_NOTE_NOT_FOUND


def test_an_unreachable_note_service_is_not_reported_as_an_ehr_failure(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """Two systems, two codes: an operator has to know which one to go and look at."""
    upstreams.fail("/ehr-reference", httpx.ConnectError("refused"))

    response = write(client)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_NOTE_SERVICE_UNAVAILABLE


def test_an_unreachable_ehr_keeps_its_own_code(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    upstreams.fail("/DocumentReference", httpx.Response(503))

    response = write(client)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_UPSTREAM_UNAVAILABLE


def test_a_failure_to_record_reports_the_document_that_exists(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """The one failure where the chart already changed.

    Reporting it as a plain error would invite a retry, and the retry would file
    a second copy — so the id is in the message and the message says not to.
    """

    # Keyed on the method rather than the path: the read and the record share a
    # path, and only the record may fail here.
    def failing_patch(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(500)
        return upstreams.handler(request)

    client.app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(failing_patch)
    )

    response = write(client)

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == fhir_routes.ERROR_CODE_RECORD_FAILED
    assert DOCUMENT_ID in error["message"]
    assert "do not retry" in error["message"].lower()


def test_a_failure_to_record_still_audited_the_disclosure(
    client: TestClient, upstreams: FakeUpstreams, audit: AuditRecorder
) -> None:
    """The note reached the chart, so the trail says so even though the record failed."""

    def failing_patch(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(500)
        return upstreams.handler(request)

    client.app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(failing_patch)
    )

    write(client)

    assert [call["action"] for call in audit.calls] == [AuditAction.WRITE_NOTE_TO_EHR]


def test_a_body_naming_anything_but_a_session_is_refused(client: TestClient) -> None:
    """No ``ehr_encounter_id`` from a client: the chart entry is resolved server-side."""
    response = client.post(
        "/fhir/notes",
        json={"session_id": SESSION_ID, "ehr_encounter_id": "encounter-99"},
        headers=HEADERS,
    )

    assert response.status_code == 422


def test_the_launch_header_is_required(client: TestClient) -> None:
    """Keyed on launch_id like every route here; the body carries the session."""
    response = client.post("/fhir/notes", json={"session_id": SESSION_ID})

    assert response.status_code == 422


def test_an_unknown_launch_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/fhir/notes",
        json={"session_id": SESSION_ID},
        headers={fhir_routes.LAUNCH_ID_HEADER: str(uuid.uuid4())},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_UNKNOWN_LAUNCH

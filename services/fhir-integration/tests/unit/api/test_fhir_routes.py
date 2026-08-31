"""``GET /fhir/patient/{id}/context`` and ``GET /fhir/encounter/{id}``. TASK-052.

What is asserted here beyond the happy path is mostly about identity and
auditing: that the routes are keyed on ``launch_id`` and cannot be talked into
accepting a ``session_id``, that each call writes exactly one audit row with a
null actor, and that each FHIR-layer failure keeps its own status and code.
"""

from __future__ import annotations

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
from src.main import create_app
from src.smart import store
from src.smart.store import LaunchToken
from tests.unit.api.conftest import FakeRedis
from tests.unit.conftest import ACCESS_TOKEN, FHIR_BASE_URL, FakeFHIRServer, coverage_resource

LAUNCH_ID = "3f2a7c18-0d64-4a51-9f0e-8b1c2d3e4f50"
HEADERS = {fhir_routes.LAUNCH_ID_HEADER: LAUNCH_ID}


class AuditRecorder:
    """Records what ``audit_log`` was called with, instead of writing a row."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> AuditRecorder:
    """Replace the audit write.

    Patching here is not the shortcut CLAUDE.md warns about for AWS mocking:
    this is our own function and the assertion is on *our* call to it, which is
    the behaviour the task specifies. Nothing about a third-party service is
    being stubbed out and then asserted as if it had answered.
    """
    recorder = AuditRecorder()
    monkeypatch.setattr("src.audit.audit_log", recorder)
    return recorder


@pytest.fixture
def ehr_server() -> FakeFHIRServer:
    return FakeFHIRServer()


@pytest.fixture
def redis_with_launch() -> FakeRedis:
    """A Redis holding one launch's stored EHR token."""
    fake = FakeRedis()
    token = LaunchToken(
        ehr_type=EHRType.GENERIC,
        fhir_base_url=FHIR_BASE_URL,
        access_token=ACCESS_TOKEN,
        patient_id="synthea-123",
        encounter_id="encounter-1",
    )
    fake.values[store.token_key(LAUNCH_ID)] = token.model_dump_json()
    return fake


@pytest.fixture
def client(redis_with_launch: FakeRedis, ehr_server: FakeFHIRServer) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: redis_with_launch
    http = ehr_server.client()
    app.dependency_overrides[get_http_client] = lambda: http
    with TestClient(app) as test_client:
        yield test_client


class TestPatientContext:
    def test_it_returns_the_assembled_context(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        response = client.get("/fhir/patient/synthea-123/context", headers=HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["error"] is None
        assert body["data"]["patient"]["family_name"] == "Sanchez"
        assert body["data"]["coverage"]["payer"] == "Aetna Better Health of MA"
        assert body["data"]["requires_manual_confirmation"] is False

    def test_incomplete_coverage_is_a_200_with_the_flag_set(
        self, client: TestClient, ehr_server: FakeFHIRServer, audit: AuditRecorder
    ) -> None:
        """Never a failure — a provider filling the payer in is a working encounter."""
        ehr_server.coverages = [coverage_resource(plan_type_text=None)]

        response = client.get("/fhir/patient/synthea-123/context", headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["data"]["requires_manual_confirmation"] is True
        assert response.json()["data"]["coverage"]["plan_type"] is None


class TestEncounter:
    def test_it_returns_the_r4_resource(self, client: TestClient, audit: AuditRecorder) -> None:
        response = client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["data"]["id"] == "encounter-1"

    def test_it_carries_no_manual_confirmation_flag(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        """That flag belongs to a patient context, not to an encounter read."""
        body = client.get("/fhir/encounter/encounter-1", headers=HEADERS).json()

        assert "requires_manual_confirmation" not in body["data"]


class TestTheLaunchIdentifier:
    def test_a_missing_header_is_a_422(self, client: TestClient) -> None:
        response = client.get("/fhir/patient/synthea-123/context")

        assert response.status_code == 422

    def test_an_unknown_launch_id_is_a_404(self, client: TestClient) -> None:
        """Not a 401 — nothing is being rejected, there is simply no such launch."""
        response = client.get(
            "/fhir/patient/synthea-123/context",
            headers={fhir_routes.LAUNCH_ID_HEADER: str(uuid.uuid4())},
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "FHIR_UNKNOWN_LAUNCH"

    def test_a_session_id_in_the_header_is_a_404(
        self, client: TestClient, redis_with_launch: FakeRedis
    ) -> None:
        """CLAUDE.md, "A SMART launch is not an encounter session".

        The two identifiers are different values with different lifetimes and
        neither is derivable from the other. A ``session_id`` presented here
        must not resolve to anything — and no fallback may ever be added that
        tries it as the other kind of identifier.
        """
        session_id = str(uuid.uuid4())
        # A session key exists in Redis; it must not be reachable from here.
        redis_with_launch.values[f"session:{session_id}"] = "{}"

        response = client.get(
            "/fhir/patient/synthea-123/context",
            headers={fhir_routes.LAUNCH_ID_HEADER: session_id},
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "FHIR_UNKNOWN_LAUNCH"

    def test_the_presented_identifier_is_not_logged(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        launch_id = str(uuid.uuid4())
        with caplog.at_level("DEBUG"):
            client.get(
                "/fhir/patient/synthea-123/context",
                headers={fhir_routes.LAUNCH_ID_HEADER: launch_id},
            )

        assert launch_id not in caplog.text

    def test_the_access_token_never_reaches_a_log_record(
        self, client: TestClient, audit: AuditRecorder, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG"):
            client.get("/fhir/patient/synthea-123/context", headers=HEADERS)

        assert ACCESS_TOKEN not in caplog.text


class TestFailureOutcomesStayDistinct:
    @pytest.mark.parametrize(
        ("outcome", "status", "code"),
        [
            (httpx.Response(404), 404, "FHIR_RESOURCE_NOT_FOUND"),
            (httpx.Response(503), 502, "FHIR_UPSTREAM_UNAVAILABLE"),
            (httpx.ReadTimeout("slow"), 504, "FHIR_UPSTREAM_UNAVAILABLE"),
            (httpx.Response(200, text="not json"), 502, "FHIR_MALFORMED_RESPONSE"),
            (httpx.Response(401), 401, "FHIR_LAUNCH_EXPIRED"),
        ],
    )
    def test_each_failure_keeps_its_own_status_and_code(
        self,
        client: TestClient,
        ehr_server: FakeFHIRServer,
        outcome: httpx.Response | Exception,
        status: int,
        code: str,
    ) -> None:
        """The distinction is the point: only the 502/504 pair is worth retrying."""
        ehr_server.fail("/Patient/", outcome)

        response = client.get("/fhir/patient/synthea-123/context", headers=HEADERS)

        assert response.status_code == status
        assert response.json()["error"]["code"] == code

    def test_the_expired_launch_error_names_repeating_the_launch(
        self, client: TestClient, ehr_server: FakeFHIRServer
    ) -> None:
        """Until TASK-051b, that is the only remedy, and it is stated not implied."""
        ehr_server.fail("/Patient/", httpx.Response(401))

        message = client.get("/fhir/patient/synthea-123/context", headers=HEADERS).json()["error"][
            "message"
        ]

        assert "launch must be repeated" in message

    def test_no_operation_outcome_diagnostics_reaches_the_response(
        self, client: TestClient, ehr_server: FakeFHIRServer
    ) -> None:
        ehr_server.fail(
            "/Patient/",
            httpx.Response(
                404,
                json={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "not-found",
                            "diagnostics": "No patient Aurelio Sanchez born 1962-04-17",
                        }
                    ],
                },
            ),
        )

        body = client.get("/fhir/patient/synthea-123/context", headers=HEADERS).text

        assert "Aurelio" not in body
        assert "1962" not in body


class TestAuditing:
    def test_one_row_per_call_not_one_per_fhir_fetch(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        """The context makes three round trips; the auditable access is the read.

        A row per fetch would bury the event an audit is actually asked about
        under per-fetch noise — the same "one row per unit of work" rule the
        Redis consumers follow.
        """
        client.get("/fhir/patient/synthea-123/context", headers=HEADERS)

        assert len(audit.calls) == 1
        assert audit.calls[0]["action"] is AuditAction.READ_PATIENT
        assert audit.calls[0]["resource_type"] == "Patient"
        assert audit.calls[0]["resource_id"] == "synthea-123"

    def test_the_encounter_read_audits_as_read_encounter(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        client.get("/fhir/encounter/encounter-1", headers=HEADERS)

        assert len(audit.calls) == 1
        assert audit.calls[0]["action"] is AuditAction.READ_ENCOUNTER
        assert audit.calls[0]["resource_id"] == "encounter-1"

    def test_the_actor_is_null_rather_than_invented(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        """No encounters row exists at launch time, so there is no provider to read.

        A fabricated service-account UUID would look like a real actor in the one
        table an auditor reads to answer "who accessed patient X". TASK-051c is
        what fills this from the SMART fhirUser claim.
        """
        client.get("/fhir/patient/synthea-123/context", headers=HEADERS)

        assert audit.calls[0]["actor_id"] is None
        assert audit.calls[0]["session_id"] is None
        assert audit.calls[0]["service_name"] == "fhir-integration"

    def test_a_failed_read_writes_no_audit_row(
        self, client: TestClient, ehr_server: FakeFHIRServer, audit: AuditRecorder
    ) -> None:
        """Nothing was disclosed, so there is no access to record."""
        ehr_server.fail("/Patient/", httpx.Response(503))

        client.get("/fhir/patient/synthea-123/context", headers=HEADERS)

        assert audit.calls == []

    def test_an_unknown_launch_writes_no_audit_row(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        client.get(
            "/fhir/patient/synthea-123/context",
            headers={fhir_routes.LAUNCH_ID_HEADER: str(uuid.uuid4())},
        )

        assert audit.calls == []

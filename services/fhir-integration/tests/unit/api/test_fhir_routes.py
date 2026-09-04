"""The three ``/fhir`` read routes. TASK-052, and TASK-052b's coverage context.

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
from src.adapters.base import PATIENT_SEARCH_LIMIT
from src.adapters.factory import EHRType
from src.api import fhir as fhir_routes
from src.api.dependencies import get_http_client, get_redis
from src.main import create_app
from src.providers_client import ProviderServiceError
from src.smart import store
from src.smart.store import LaunchToken
from tests.unit.api.conftest import TOKEN_ENDPOINT, FakeRedis
from tests.unit.conftest import (
    ACCESS_TOKEN,
    FHIR_BASE_URL,
    FakeFHIRServer,
    coverage_resource,
    patient_resource,
)

LAUNCH_ID = "3f2a7c18-0d64-4a51-9f0e-8b1c2d3e4f50"
HEADERS = {fhir_routes.LAUNCH_ID_HEADER: LAUNCH_ID}

#: The provider the EHR said authorized the launch, as TASK-051c resolves it:
#: an absolute Practitioner reference, and deliberately not a UUID — which is
#: why it is audited in a column of its own rather than as ``actor_id``.
PRACTITIONER_REF = f"{FHIR_BASE_URL}/Practitioner/prov-77"


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
        # Comfortably inside its lifetime, so these routes are exercised without
        # renewal running at all. Renewal has its own suite.
        access_token_expires_at=store.access_token_expiry(3600),
        token_endpoint=TOKEN_ENDPOINT,
        refresh_token="ehr-refresh-token",
        patient_id="synthea-123",
        encounter_id="encounter-1",
        # Resolved from a verified id_token at callback time (TASK-051c). The
        # launch that stored this record knew who authorized it, which is the
        # ordinary case; ``redis_with_anonymous_launch`` below is the other one.
        fhir_practitioner_ref=PRACTITIONER_REF,
    )
    fake.values[store.token_key(LAUNCH_ID)] = token.model_dump_json()
    return fake


def _rewrite_launch(fake: FakeRedis, **changes: Any) -> None:
    """Store the launch record again with some of its fields changed."""
    key = store.token_key(LAUNCH_ID)
    stored = LaunchToken.model_validate_json(fake.values[key])
    fake.values[key] = stored.model_copy(update=changes).model_dump_json()


def _client_over(fake: FakeRedis, ehr_server: FakeFHIRServer) -> Iterator[TestClient]:
    """Yield a test client with Redis and the EHR replaced by these two fakes."""
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: fake
    http = ehr_server.client()
    app.dependency_overrides[get_http_client] = lambda: http
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def redis_with_anonymous_launch(redis_with_launch: FakeRedis) -> FakeRedis:
    """A launch whose id_token was absent or did not verify.

    The actor is unknown, and unknown is recorded as a null rather than as
    anything invented.
    """
    stored = LaunchToken.model_validate_json(redis_with_launch.values[store.token_key(LAUNCH_ID)])
    redis_with_launch.values[store.token_key(LAUNCH_ID)] = stored.model_copy(
        update={"fhir_practitioner_ref": None}
    ).model_dump_json()
    return redis_with_launch


@pytest.fixture
def anonymous_client(
    redis_with_anonymous_launch: FakeRedis, ehr_server: FakeFHIRServer
) -> Iterator[TestClient]:
    """A client whose launch record carries no verified actor."""
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: redis_with_anonymous_launch
    http = ehr_server.client()
    app.dependency_overrides[get_http_client] = lambda: http
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def standalone_launch_client(
    redis_with_launch: FakeRedis, ehr_server: FakeFHIRServer
) -> Iterator[TestClient]:
    """A launch the EHR gave a patient and no encounter.

    This is what a standalone launch looks like on the record: the provider
    opened MedAuth outside a chart, so there is a patient in scope and no visit
    to name. TASK-051d's route has to treat it as a working launch.
    """
    _rewrite_launch(redis_with_launch, encounter_id=None)
    yield from _client_over(redis_with_launch, ehr_server)


@pytest.fixture
def contextless_launch_client(
    redis_with_launch: FakeRedis, ehr_server: FakeFHIRServer
) -> Iterator[TestClient]:
    """A launch that carried no patient context at all.

    ``LaunchToken`` allows it, so the route has to answer it — and what it has
    to answer is "this launch named nobody", which is not the same fact as "no
    such launch".
    """
    _rewrite_launch(redis_with_launch, patient_id=None, encounter_id=None)
    yield from _client_over(redis_with_launch, ehr_server)


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

    def test_the_ehr_asserted_actor_is_recorded_in_its_own_column(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        """TASK-051c, and the inverse of what this test used to assert.

        It previously read "the actor is null rather than invented" and named
        TASK-051c as what would fill ``actor_id``. That was never possible: a
        Practitioner id is usually not a UUID and that column refuses one. So
        ``actor_id`` stays null **permanently** and the identity the EHR
        asserted is recorded beside it. See CLAUDE.md, "The EHR-asserted actor
        is its own column".
        """
        client.get("/fhir/patient/synthea-123/context", headers=HEADERS)

        call = audit.calls[0]
        assert call["fhir_practitioner_ref"] == PRACTITIONER_REF
        # Not waiting on anything: no encounters row exists at launch time, and
        # a fabricated service-account UUID would look like a real actor in the
        # one table an auditor reads to answer "who accessed patient X".
        assert call["actor_id"] is None
        # A launch is not an encounter session, so nothing will ever fill this.
        assert call["session_id"] is None
        assert call["service_name"] == "fhir-integration"

    def test_an_unverified_launch_records_no_actor_at_all(
        self, anonymous_client: TestClient, audit: AuditRecorder
    ) -> None:
        """No id_token, or one that did not verify.

        Both actor columns stay null. Falling back to the unverified claim is
        the fabrication TASK-051c exists to remove, one step subtler than a
        service-account UUID.
        """
        anonymous_client.get("/fhir/patient/synthea-123/context", headers=HEADERS)

        call = audit.calls[0]
        assert call["fhir_practitioner_ref"] is None
        assert call["actor_id"] is None

    def test_the_read_still_happens_without_a_known_actor(
        self, anonymous_client: TestClient, audit: AuditRecorder
    ) -> None:
        """An unknown actor is audited, never a reason to refuse the read."""
        response = anonymous_client.get("/fhir/patient/synthea-123/context", headers=HEADERS)

        assert response.status_code == 200

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


class TestEncounterCoverageContext:
    """TASK-052b's route — what ``track-a-clinical`` calls at ``POST /sessions/start``."""

    def test_it_returns_the_payer_half_and_the_site_of_care(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        response = client.get("/fhir/encounter/encounter-1/coverage-context", headers=HEADERS)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["encounter_id"] == "encounter-1"
        assert data["patient_id"] == "synthea-123"
        assert data["coverage"]["payer"] == "Aetna Better Health of MA"
        assert data["coverage"]["plan_type"] == "PPO"
        assert data["state"] == "MA"
        assert data["requires_manual_confirmation"] is False

    def test_an_incomplete_answer_is_a_200_with_null_columns(
        self, client: TestClient, ehr_server: FakeFHIRServer, audit: AuditRecorder
    ) -> None:
        """A NULL column is the correct record of something the EHR did not hold.

        The dispatcher downstream names exactly which fields are still missing;
        a guessed payer would instead write a real policy answer under a
        ``rag:`` key standing for a plan the patient is not on.
        """
        ehr_server.coverages = []

        response = client.get("/fhir/encounter/encounter-1/coverage-context", headers=HEADERS)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["coverage"] is None
        assert data["requires_manual_confirmation"] is True

    def test_it_writes_one_audit_row_carrying_the_ehr_asserted_actor(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        """One row per call, not one per FHIR fetch — and this call makes four.

        ``actor_id`` is permanently null here and the provider the EHR asserted
        is recorded in ``fhir_practitioner_ref`` (TASK-051c). This test named
        TASK-051c as what would fill ``actor_id``; that is not what it does, and
        the assertion is inverted rather than a new one written beside it.
        """
        client.get("/fhir/encounter/encounter-1/coverage-context", headers=HEADERS)

        assert len(audit.calls) == 1
        call = audit.calls[0]
        assert call["action"] is AuditAction.READ_PATIENT
        assert call["resource_type"] == "Encounter"
        assert call["resource_id"] == "encounter-1"
        assert call["fhir_practitioner_ref"] == PRACTITIONER_REF
        assert call["actor_id"] is None
        assert call["session_id"] is None

    def test_an_unknown_launch_is_a_404(self, client: TestClient) -> None:
        """Including a ``session_id`` sent here by mistake. Nothing is rejected —
        there is simply no such launch."""
        response = client.get(
            "/fhir/encounter/encounter-1/coverage-context",
            headers={fhir_routes.LAUNCH_ID_HEADER: str(uuid.uuid4())},
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_UNKNOWN_LAUNCH

    def test_an_unknown_encounter_is_a_404(
        self, client: TestClient, ehr_server: FakeFHIRServer
    ) -> None:
        ehr_server.encounter = None

        response = client.get("/fhir/encounter/encounter-1/coverage-context", headers=HEADERS)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_NOT_FOUND

    def test_an_ehr_outage_is_a_502_and_writes_no_audit_row(
        self, client: TestClient, ehr_server: FakeFHIRServer, audit: AuditRecorder
    ) -> None:
        """No PHI was read, so there is nothing to record."""
        ehr_server.fail("/Encounter/", httpx.Response(503))

        response = client.get("/fhir/encounter/encounter-1/coverage-context", headers=HEADERS)

        assert response.status_code == 502
        assert audit.calls == []

    def test_the_response_never_names_an_encounter_session(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        """CLAUDE.md, "A SMART launch is not an encounter session".

        ``encounter_id`` here is the *EHR's* Encounter id, not this platform's
        ``session_id`` and not the ``encounters`` primary key. A ``session_id``
        appearing on this payload is how the two identifiers would quietly
        become one, and this route is the seam where that would happen: it is
        the only one both services hold a value from.
        """
        data = client.get("/fhir/encounter/encounter-1/coverage-context", headers=HEADERS).json()[
            "data"
        ]

        assert "session_id" not in data
        assert "launch_id" not in data


class TestLaunchContext:
    """TASK-051d's route — what a client calls before it can start a visit.

    The route reads no chart. What is asserted here is mostly that it discloses
    exactly two identifiers and nothing else, that a launch with no encounter is
    a working launch rather than a missing one, and that the disclosure is
    audited like any other PHI read.
    """

    def test_an_ehr_launch_returns_both_identifiers_and_audits(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        response = client.get("/fhir/launch-context", headers=HEADERS)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["patient_id"] == "synthea-123"
        assert data["encounter_id"] == "encounter-1"

        assert len(audit.calls) == 1
        call = audit.calls[0]
        assert call["action"] is AuditAction.READ_PATIENT
        assert call["resource_type"] == "Patient"
        assert call["resource_id"] == "synthea-123"
        assert call["fhir_practitioner_ref"] == PRACTITIONER_REF
        assert call["actor_id"] is None
        assert call["session_id"] is None

    def test_a_standalone_launch_is_a_200_with_a_null_encounter(
        self, standalone_launch_client: TestClient, audit: AuditRecorder
    ) -> None:
        """A launch with a patient and no encounter is working, not broken.

        Answering 404 would tell a client to repeat a launch that succeeded —
        the same conflation as reading a payer's silence as a negative
        determination. The client starts a session without an encounter and
        leaves the payer columns NULL, which the dispatcher reports per
        procedure.
        """
        response = standalone_launch_client.get("/fhir/launch-context", headers=HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["error"] is None
        assert body["data"]["patient_id"] == "synthea-123"
        assert body["data"]["encounter_id"] is None

    def test_a_standalone_launch_still_audits_the_patient(
        self, standalone_launch_client: TestClient, audit: AuditRecorder
    ) -> None:
        """The patient is what makes this a disclosure; the encounter is not."""
        standalone_launch_client.get("/fhir/launch-context", headers=HEADERS)

        assert len(audit.calls) == 1
        assert audit.calls[0]["resource_id"] == "synthea-123"

    def test_a_launch_carrying_no_patient_discloses_nothing_and_audits_nothing(
        self, contextless_launch_client: TestClient, audit: AuditRecorder
    ) -> None:
        """Both nulls is a 200, and no access happened, so no row records one.

        A launch may carry no patient context at all. That is still a launch, so
        it is not a 404 — and nothing was disclosed, so an audit row naming no
        resource would record an access that never took place.
        """
        response = contextless_launch_client.get("/fhir/launch-context", headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["data"] == {
            "patient_id": None,
            "encounter_id": None,
            # No registry is configured on this client, so the resolution fails
            # and the launch still answers. See TestLaunchContextProvider.
            "provider_id": None,
        }
        assert audit.calls == []

    def test_an_unverified_launch_records_no_actor(
        self, anonymous_client: TestClient, audit: AuditRecorder
    ) -> None:
        """Both actor columns stay null rather than carrying the unverified claim."""
        anonymous_client.get("/fhir/launch-context", headers=HEADERS)

        assert audit.calls[0]["fhir_practitioner_ref"] is None
        assert audit.calls[0]["actor_id"] is None

    @pytest.mark.parametrize("launch_id", ["unknown", "expired", "consumed"])
    def test_a_launch_id_with_no_record_is_a_404(
        self, client: TestClient, audit: AuditRecorder, launch_id: str
    ) -> None:
        """Unknown, expired and already-rejected are one answer: there is no record.

        Nothing distinguishes them at the Redis layer by design — a record that
        expired, one that never existed, and one dropped after its grant was
        refused all simply fail to load. None of them is a credential being
        rejected, so none of them is a 401.
        """
        response = client.get(
            "/fhir/launch-context",
            headers={fhir_routes.LAUNCH_ID_HEADER: f"{launch_id}-{uuid.uuid4()}"},
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_UNKNOWN_LAUNCH
        assert audit.calls == []

    def test_a_missing_header_is_a_422(self, client: TestClient) -> None:
        response = client.get("/fhir/launch-context")

        assert response.status_code == 422

    def test_a_session_id_in_the_header_is_a_404(
        self, client: TestClient, redis_with_launch: FakeRedis
    ) -> None:
        """CLAUDE.md, "A SMART launch is not an encounter session".

        This route is the one a client calls holding both identifiers at once,
        which makes it the likeliest place to send the wrong one. It must miss,
        and no fallback may ever be added that tries it as the other kind.
        """
        session_id = str(uuid.uuid4())
        redis_with_launch.values[f"session:{session_id}"] = "{}"

        response = client.get(
            "/fhir/launch-context",
            headers={fhir_routes.LAUNCH_ID_HEADER: session_id},
        )

        assert response.status_code == 404

    def test_the_response_carries_no_credential(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        """The stored record holds three secrets beside the context; none ships.

        Asserted against the response text rather than its keys, so a token
        nested inside some future field cannot pass this by not being a
        top-level name.
        """
        response = client.get("/fhir/launch-context", headers=HEADERS)

        assert set(response.json()["data"]) == {"patient_id", "encounter_id", "provider_id"}
        assert ACCESS_TOKEN not in response.text
        assert "ehr-refresh-token" not in response.text
        assert "scope" not in response.text
        # Nor the practitioner reference the provider id was resolved from: a
        # client receives an opaque local identifier and cannot assert a provider
        # identity of its own.
        assert PRACTITIONER_REF not in response.text

    def test_the_launch_id_reaches_no_log_line(
        self, client: TestClient, audit: AuditRecorder, caplog: pytest.LogCaptureFixture
    ) -> None:
        """It resolves to an EHR access token, so it is a credential in a log."""
        with caplog.at_level("DEBUG"):
            client.get("/fhir/launch-context", headers=HEADERS)

        assert LAUNCH_ID not in caplog.text

    def test_it_reads_no_chart(
        self, client: TestClient, ehr_server: FakeFHIRServer, audit: AuditRecorder
    ) -> None:
        """The whole point of the route: the EHR has already told us this.

        Asking the EHR again would spend a token, add latency to the one call
        standing between a launch and a visit, and could fail where a stored
        answer cannot.
        """
        response = client.get("/fhir/launch-context", headers=HEADERS)

        assert response.status_code == 200
        assert ehr_server.requested_paths == []


class FakeProviders:
    """Stands in for the registry in ``track-a-clinical``.

    Records what it was asked to resolve, so a test can assert the *verified*
    reference reached it and that nothing else did.
    """

    def __init__(self, provider_id: str | None = None, error: Exception | None = None) -> None:
        self.provider_id = provider_id or "8f14e45f-ceea-467a-9c0e-1b2a3c4d5e6f"
        self.error = error
        self.resolved: list[str] = []

    async def resolve(self, fhir_practitioner_ref: str) -> str:
        self.resolved.append(fhir_practitioner_ref)
        if self.error is not None:
            raise self.error
        return self.provider_id


def with_providers(test_client: TestClient, providers: FakeProviders) -> TestClient:
    """Point a launch-context route at a fake registry."""
    test_client.app.dependency_overrides[fhir_routes.get_providers_client] = lambda: providers
    return test_client


class TestLaunchContextProvider:
    """Resolving a launch's practitioner into a ``provider_id`` (TASK-025b).

    ``POST /sessions/start`` needs a UUID and a FHIR ``Practitioner`` id is not
    one, so the launch context carries the resolved identifier rather than the
    reference. What matters here is which reference reaches the registry, and
    that neither an unverified actor nor an unreachable registry breaks a launch
    that is otherwise working.
    """

    def test_it_resolves_the_verified_practitioner_and_returns_the_provider_id(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        providers = FakeProviders()

        response = with_providers(client, providers).get("/fhir/launch-context", headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["data"]["provider_id"] == providers.provider_id
        # The absolute reference, not a bare id: Practitioner/1 on two servers is
        # two different people.
        assert providers.resolved == [PRACTITIONER_REF]

    def test_an_unverified_actor_resolves_to_no_provider_and_asks_nothing(
        self, anonymous_client: TestClient, audit: AuditRecorder
    ) -> None:
        """A claim we could not verify never becomes a provider row.

        Registering one would put a fabricated identity in the column an auditor
        reads to answer who saw this patient — the same fabrication the
        null-over-invention rule refuses one table over. The registry is not even
        asked, so nothing can be created by accident.
        """
        providers = FakeProviders()

        response = with_providers(anonymous_client, providers).get(
            "/fhir/launch-context", headers=HEADERS
        )

        assert response.status_code == 200
        assert response.json()["data"]["provider_id"] is None
        assert providers.resolved == []

    def test_an_unreachable_registry_is_a_null_provider_not_a_failed_launch(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        """The patient half is what the caller mainly came for, and it is intact.

        Failing the whole route because a sibling service is restarting would
        make a launch that is working perfectly look expired — the same
        conflation this repository refuses when a payer silence is read as a
        negative determination.
        """
        providers = FakeProviders(error=ProviderServiceError("track-a-clinical is not configured"))

        response = with_providers(client, providers).get("/fhir/launch-context", headers=HEADERS)

        assert response.status_code == 200
        body = response.json()["data"]
        assert body["provider_id"] is None
        assert body["patient_id"] == "synthea-123"

    def test_a_failed_resolution_never_logs_the_practitioner_reference(
        self, client: TestClient, audit: AuditRecorder, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The reference names an individual clinician; the warning names the failure."""
        providers = FakeProviders(
            error=ProviderServiceError("track-a-clinical could not be reached")
        )

        with caplog.at_level("DEBUG"):
            with_providers(client, providers).get("/fhir/launch-context", headers=HEADERS)

        assert PRACTITIONER_REF not in caplog.text
        assert "prov-77" not in caplog.text
        assert "could not be reached" in caplog.text


class TestPatientSearch:
    """``GET /fhir/patient/search`` — the standalone-launch half (TASK-025b).

    After an EHR launch the patient is the one the EHR named and
    ``/fhir/launch-context`` returns them; this route answers the case where
    nobody told us who is in the room. The assertions that matter are about what
    is disclosed and audited, and about a truncated result being reported rather
    than silently short.
    """

    def test_it_returns_the_matches(
        self, client: TestClient, audit: AuditRecorder, ehr_server: FakeFHIRServer
    ) -> None:
        response = client.get("/fhir/patient/search", params={"query": "Sanchez"}, headers=HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["error"] is None
        assert body["data"]["truncated"] is False
        assert body["data"]["matches"] == [
            {
                "patient_id": "synthea-123",
                "family_name": "Sanchez",
                "given_names": ["Aurelio", "Luis"],
                "birth_date": "1962-04-17",
                "gender": "male",
            }
        ]

    def test_it_discloses_less_per_candidate_than_a_patient_read(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        """Minimum necessary, applied to a list rather than to a record.

        Every field here is disclosed for every candidate, including the people
        who are not the patient in the room, so the shape is narrower than
        ``PatientInfo`` — ``address_state`` in particular has no reader on a
        picker screen.
        """
        response = client.get("/fhir/patient/search", params={"query": "Sanchez"}, headers=HEADERS)

        assert "address_state" not in response.json()["data"]["matches"][0]

    def test_it_searches_by_name_and_not_by_a_full_text_parameter(
        self, client: TestClient, audit: AuditRecorder, ehr_server: FakeFHIRServer
    ) -> None:
        """US Core requires ``Patient?name=``; ``_content`` and ``_text`` are optional.

        A free-text parameter would work on some vendors and silently return
        nothing on others, which is indistinguishable from a genuine no-match —
        the failure mode this repository refuses everywhere.
        """
        client.get("/fhir/patient/search", params={"query": "Sanchez"}, headers=HEADERS)

        searched = ehr_server.requested_paths[-1]
        assert "name=Sanchez" in searched
        assert "_content" not in searched
        assert "_text" not in searched

    def test_a_birth_date_narrows_the_search(
        self, client: TestClient, audit: AuditRecorder, ehr_server: FakeFHIRServer
    ) -> None:
        """Two people in a practice share a name far more often than a name and a DOB."""
        client.get(
            "/fhir/patient/search",
            params={"query": "Sanchez", "birth_date": "1962-04-17"},
            headers=HEADERS,
        )

        assert "birthdate=1962-04-17" in ehr_server.requested_paths[-1]

    def test_matching_nobody_is_an_empty_200_and_audits_nothing(
        self, client: TestClient, audit: AuditRecorder, ehr_server: FakeFHIRServer
    ) -> None:
        """Nobody by that name, and no such launch, are different facts.

        A 404 here would tell a client to repeat a launch that is working. And
        nothing was disclosed, so no row records a disclosure.
        """
        ehr_server.patient_matches = []

        response = client.get("/fhir/patient/search", params={"query": "Nobody"}, headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["data"] == {"matches": [], "truncated": False}
        assert audit.calls == []

    def test_it_audits_one_row_per_disclosed_match(
        self, client: TestClient, audit: AuditRecorder, ehr_server: FakeFHIRServer
    ) -> None:
        """Three matches disclosed three identifiers, two of them not the patient.

        A single row naming none of them would make those disclosures invisible
        to the one query the audit table exists to answer.
        """
        ehr_server.patient_matches = [patient_resource(f"synthea-{index}") for index in range(3)]

        client.get("/fhir/patient/search", params={"query": "Sanchez"}, headers=HEADERS)

        assert [call["action"] for call in audit.calls] == [AuditAction.READ_PATIENT] * 3
        assert [call["resource_id"] for call in audit.calls] == [
            "synthea-0",
            "synthea-1",
            "synthea-2",
        ]
        assert {call["fhir_practitioner_ref"] for call in audit.calls} == {PRACTITIONER_REF}
        assert {call["actor_id"] for call in audit.calls} == {None}

    def test_more_matches_than_fit_are_reported_and_capped(
        self, client: TestClient, audit: AuditRecorder, ehr_server: FakeFHIRServer
    ) -> None:
        """A provider shown 20 of 200 Smiths has to be told there were more.

        Otherwise they conclude the patient they want is not in the system. Same
        rule as the transcript-limit one: report reduced coverage, never truncate
        in silence.
        """
        ehr_server.patient_matches = [
            patient_resource(f"synthea-{index}") for index in range(PATIENT_SEARCH_LIMIT + 5)
        ]

        response = client.get("/fhir/patient/search", params={"query": "Smith"}, headers=HEADERS)

        body = response.json()["data"]
        assert body["truncated"] is True
        assert len(body["matches"]) == PATIENT_SEARCH_LIMIT
        # One row per patient actually disclosed, not per patient the EHR held.
        assert len(audit.calls) == PATIENT_SEARCH_LIMIT

    def test_a_search_warning_entry_is_not_a_candidate(
        self, client: TestClient, audit: AuditRecorder, ehr_server: FakeFHIRServer
    ) -> None:
        """A searchset Bundle may carry OperationOutcome entries beside the matches."""
        ehr_server.patient_matches = [
            {"resourceType": "OperationOutcome", "issue": []},
            patient_resource("synthea-9"),
        ]

        response = client.get("/fhir/patient/search", params={"query": "Sanchez"}, headers=HEADERS)

        matches = response.json()["data"]["matches"]
        assert [match["patient_id"] for match in matches] == ["synthea-9"]

    def test_the_query_reaches_no_log_line_this_service_writes(
        self,
        client: TestClient,
        audit: AuditRecorder,
        ehr_server: FakeFHIRServer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A patient name is patient content, and this is an operational log.

        Driven through the truncation branch on purpose: that is the one place
        this route logs anything at all, so asserting against a clean search
        would pass by having nothing to inspect. The line reports the count and
        never the terms.

        **Scoped to this repository's own loggers, and that scope is a finding
        rather than a convenience.** `httpx` logs every request it makes at INFO
        including the full URL, so `Patient?name=Sanchez` reaches stdout from the
        library. That is real, and it predates this route — `_search` has been
        issuing `Coverage?patient={id}` since TASK-052 — so it is **TASK-046**
        rather than something this test can assert away. The test below pins the
        gap, so nobody reads this narrowing as "checked and clean".
        """
        ehr_server.patient_matches = [
            patient_resource(f"synthea-{index}") for index in range(PATIENT_SEARCH_LIMIT + 1)
        ]

        with caplog.at_level("DEBUG"):
            client.get("/fhir/patient/search", params={"query": "Sanchez"}, headers=HEADERS)

        ours = [record for record in caplog.records if record.name.startswith("src.")]
        assert ours, "expected the truncation branch to log"
        assert all("Sanchez" not in record.getMessage() for record in ours)

    def test_the_httpx_logger_still_carries_the_query_and_that_is_task_046(
        self, client: TestClient, audit: AuditRecorder, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pins the known gap, so closing it is visible rather than silent.

        When TASK-046 configures the library's logger this test fails and is
        deleted in that change — which is the point of writing the gap down as an
        assertion instead of as a comment.
        """
        with caplog.at_level("INFO"):
            client.get("/fhir/patient/search", params={"query": "Sanchez"}, headers=HEADERS)

        library = [record for record in caplog.records if record.name.startswith("httpx")]
        assert any("Sanchez" in record.getMessage() for record in library)

    def test_an_unreachable_ehr_keeps_its_own_status_and_code(
        self, client: TestClient, audit: AuditRecorder, ehr_server: FakeFHIRServer
    ) -> None:
        """Unreachable is the only one of the three outcomes worth retrying."""
        ehr_server.fail("/Patient", httpx.Response(503))

        response = client.get("/fhir/patient/search", params={"query": "Sanchez"}, headers=HEADERS)

        assert response.status_code == 502
        assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_UPSTREAM_UNAVAILABLE
        assert audit.calls == []

    def test_a_non_bundle_answer_is_malformed_rather_than_empty(
        self, client: TestClient, audit: AuditRecorder, ehr_server: FakeFHIRServer
    ) -> None:
        """A 200 that is not a searchset is a vendor quirk, not a no-match."""
        ehr_server.fail("/Patient", httpx.Response(200, json={"resourceType": "Patient"}))

        response = client.get("/fhir/patient/search", params={"query": "Sanchez"}, headers=HEADERS)

        assert response.status_code == 502
        assert response.json()["error"]["code"] == fhir_routes.ERROR_CODE_MALFORMED

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"query": ""},
            {"query": "x" * 101},
            {"query": "Sanchez", "birth_date": "17-04-1962"},
        ],
        ids=["missing", "empty", "too-long", "malformed-birth-date"],
    )
    def test_it_refuses_a_query_it_cannot_run(
        self, client: TestClient, audit: AuditRecorder, params: dict[str, str]
    ) -> None:
        response = client.get("/fhir/patient/search", params=params, headers=HEADERS)

        assert response.status_code == 422
        assert audit.calls == []

    def test_it_is_keyed_on_the_launch_like_every_other_route_here(
        self, client: TestClient, audit: AuditRecorder
    ) -> None:
        """No launch, no search: the EHR credential comes from the launch record."""
        response = client.get("/fhir/patient/search", params={"query": "Sanchez"})

        assert response.status_code == 422

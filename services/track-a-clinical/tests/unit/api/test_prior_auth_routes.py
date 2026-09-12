"""The prior-authorization routes: what they hand a submitter, and what they refuse.

TASK-054. Three properties here are rules rather than implementation details:

* **A request the payer is holding is never submitted again.** A repeat is
  refused, because a payer that receives one request twice may open two reviews.
  A request the payer *denied* or *refused to take in* may be resubmitted
  (TASK-061), and that is a new attempt row rather than an overwrite of what the
  payer said the first time — the distinction the old ``submitted_at IS NULL``
  guard could not draw.
* **The refusal is decided by the update, not by a read before it.** A
  check-then-write leaves a window in which two callers both pass the check.
* **A payer's refusal is recorded as a refusal.** An ``error`` outcome does not
  leave the row saying ``submitted``, which would have someone waiting for a
  decision on a request the payer never took in.

Backed by fakes rather than PostgreSQL, like the note tests next to it: this file
is about the request/response contract.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Select, Update

from hipaa_logger import AuditAction
from track_a_clinical import audit, prior_auth
from track_a_clinical.api.dependencies import get_db_session
from track_a_clinical.api.prior_auth import (
    ERROR_CODE_ALREADY_SUBMITTED,
    ERROR_CODE_INVALID_CURSOR,
    ERROR_CODE_REQUEST_NOT_FOUND,
)
from track_a_clinical.main import create_app
from track_a_clinical.models import (
    PRIOR_AUTH_STATUS_DENIED,
    PRIOR_AUTH_STATUS_ERROR,
    PRIOR_AUTH_STATUS_MANUAL_REQUIRED,
    PRIOR_AUTH_STATUS_PENDING,
    PRIOR_AUTH_STATUS_SUBMITTED,
    Encounter,
    PriorAuthRequest,
)

from .test_notes import RecordedAudit, make_encounter

PAYER_REFERENCE = "AUTH-88213"


def make_request(encounter: Encounter) -> PriorAuthRequest:
    """Build a detached row as TASK-060's assembly would have written it."""
    request = PriorAuthRequest(
        encounter_id=encounter.id,
        status=PRIOR_AUTH_STATUS_PENDING,
        payer_name="Aetna",
        procedures=[{"cpt_code": "27447", "description": "total knee replacement"}],
        diagnoses=[{"code": "M17.11", "display": "OA, right knee", "source": "llm-extraction"}],
        clinical_evidence=[{"text": "12 weeks of physical therapy, no improvement"}],
    )
    request.id = uuid.uuid4()
    return request


class PriorAuthSession:
    """A session that models the conditional update rather than always succeeding.

    ``UPDATE ... RETURNING`` answers the way PostgreSQL would: an id when the
    row was in a submittable state, nothing when it was not. A fake that always
    returned an id would let the submit-once rule pass by coincidence.

    Since TASK-061 the predicate is "never submitted, **or** sitting in a
    terminal unsuccessful state", so this fake models both halves — a fake that
    still only understood ``submitted_at IS NULL`` would report the
    resubmission path as broken and the regression it exists to catch as
    working.
    """

    def __init__(self, *, encounter: Encounter | None, request: PriorAuthRequest | None) -> None:
        self.encounter = encounter
        self.request = request
        self.commits = 0
        self.rollbacks = 0
        self.refreshes = 0
        self.updates = 0
        #: The attempt rows handed to ``session.add()``. The history half of the
        #: write, which the parent row's columns cannot show.
        self.added: list[Any] = []

    async def execute(self, _statement: Any) -> Any:
        return _Result(
            None
            if self.request is None or self.encounter is None
            else (self.request, self.encounter)
        )

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def scalar(self, statement: Any) -> Any:
        if isinstance(statement, Select):
            # Counting the attempts already recorded, so the next number is
            # known. Answered from what this fake was actually given rather than
            # from a constant, or the second attempt would always be numbered 2.
            return len(self.added)

        assert isinstance(statement, Update)
        self.updates += 1
        if self.request is None:
            return None
        submittable = (
            self.request.submitted_at is None
            or self.request.status in prior_auth.RESUBMITTABLE_STATUSES
        )
        if not submittable:
            return None
        # Apply what the statement actually carries rather than what this module
        # expects: a fake that wrote its own constants could not fail on a
        # handler that sent something else.
        for column, value in statement.compile().params.items():
            setattr(self.request, column, value)
        return self.request.id

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, _instance: Any) -> None:
        self.refreshes += 1


class _Result:
    """The two methods ``load_request``'s select result is used through."""

    def __init__(self, row: tuple[PriorAuthRequest, Encounter] | None) -> None:
        self._row = row

    def one_or_none(self) -> Any:
        return None if self._row is None else _Row(self._row)


class _Row:
    def __init__(self, row: tuple[PriorAuthRequest, Encounter]) -> None:
        self._row = row

    def tuple(self) -> tuple[PriorAuthRequest, Encounter]:
        return self._row


@pytest.fixture
def encounter() -> Encounter:
    encounter = make_encounter(uuid.uuid4())
    encounter.ehr_encounter_id = "Encounter-4471"
    encounter.insurance_payer = "Aetna Better Health"
    encounter.insurance_plan_type = "PPO"
    encounter.insurance_member_id = "W123456789"
    # NOT NULL with a server default, so a row read back from PostgreSQL always
    # carries one; only a detached object built in a test can be missing it.
    encounter.started_at = datetime.datetime(2026, 2, 14, 8, 0, tzinfo=datetime.UTC)
    return encounter


@pytest.fixture
def request_row(encounter: Encounter) -> PriorAuthRequest:
    return make_request(encounter)


@pytest.fixture
def recorded_audit(monkeypatch: pytest.MonkeyPatch) -> RecordedAudit:
    recorder = RecordedAudit()
    monkeypatch.setattr(audit, "audit_prior_auth_access", recorder)
    return recorder


@pytest_asyncio.fixture
async def fake(encounter: Encounter, request_row: PriorAuthRequest) -> PriorAuthSession:
    return PriorAuthSession(encounter=encounter, request=request_row)


@pytest_asyncio.fixture
async def client(
    fake: PriorAuthSession, recorded_audit: RecordedAudit
) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: fake
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://track-a-clinical"
    ) as http:
        yield http


async def test_read_returns_what_a_submission_is_built_from(
    client: AsyncClient, request_row: PriorAuthRequest, encounter: Encounter
) -> None:
    response = await client.get(f"/prior-auth/{request_row.id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["request_id"] == str(request_row.id)
    assert data["session_id"] == str(encounter.session_id)
    assert data["patient_fhir_id"] == encounter.patient_fhir_id
    assert data["ehr_encounter_id"] == encounter.ehr_encounter_id
    assert data["insurance_member_id"] == encounter.insurance_member_id
    assert data["procedures"] == request_row.procedures
    assert data["clinical_evidence"] == request_row.clinical_evidence
    assert data["submitted_at"] is None


async def test_read_prefers_the_requests_own_payer_name(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """Both spellings name the payer; the assembly's is the one it chose to record."""
    response = await client.get(f"/prior-auth/{request_row.id}")

    assert response.json()["data"]["payer_name"] == "Aetna"


async def test_read_falls_back_to_the_encounters_payer(
    client: AsyncClient, request_row: PriorAuthRequest, encounter: Encounter
) -> None:
    request_row.payer_name = None

    response = await client.get(f"/prior-auth/{request_row.id}")

    assert response.json()["data"]["payer_name"] == encounter.insurance_payer


async def test_read_audits_as_a_prior_auth_read(
    client: AsyncClient, request_row: PriorAuthRequest, recorded_audit: RecordedAudit
) -> None:
    """The row carries note excerpts, so reading it is a PHI access."""
    await client.get(f"/prior-auth/{request_row.id}")

    assert recorded_audit.actions == [AuditAction.READ_PRIOR_AUTH]


async def test_read_takes_its_actor_from_the_encounter(
    client: AsyncClient,
    request_row: PriorAuthRequest,
    encounter: Encounter,
    recorded_audit: RecordedAudit,
) -> None:
    """Never the calling service: a service-to-service hop does not change whose visit it is."""
    await client.get(f"/prior-auth/{request_row.id}")

    assert recorded_audit.calls[0]["provider_id"] == encounter.provider_id


async def test_read_reports_a_missing_chart_link_rather_than_hiding_it(
    client: AsyncClient, request_row: PriorAuthRequest, encounter: Encounter
) -> None:
    encounter.ehr_encounter_id = None

    response = await client.get(f"/prior-auth/{request_row.id}")

    assert response.status_code == 200
    assert response.json()["data"]["ehr_encounter_id"] is None


async def test_an_unknown_request_is_a_404(recorded_audit: RecordedAudit) -> None:
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: PriorAuthSession(
        encounter=None, request=None
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://track-a-clinical"
    ) as http:
        response = await http.get(f"/prior-auth/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ERROR_CODE_REQUEST_NOT_FOUND


async def test_recording_stores_the_method_outcome_and_reference(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    response = await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={
            "submission_method": "fhir-pas",
            "outcome": "complete",
            "payer_reference_number": PAYER_REFERENCE,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["submission_method"] == "fhir-pas"
    assert data["payer_outcome"] == "complete"
    assert data["payer_reference_number"] == PAYER_REFERENCE
    assert data["status"] == PRIOR_AUTH_STATUS_SUBMITTED


async def test_a_queued_answer_is_recorded_as_queued_without_a_reference(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """``preAuthRef`` is 0..1 and is usually absent on a queued answer.

    Refusing to record it, or recording it as a completed submission, would both
    lose the one thing a follow-up needs to know.
    """
    response = await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={"submission_method": "fhir-pas", "outcome": "queued"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["payer_outcome"] == "queued"
    assert data["payer_reference_number"] is None
    assert data["submitted_at"] is not None


async def test_a_payer_refusal_does_not_leave_the_row_saying_submitted(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """The payer never took the request in, so nothing is pending with them."""
    response = await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={"submission_method": "fhir-pas", "outcome": "error"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == PRIOR_AUTH_STATUS_ERROR


async def test_recording_audits_as_a_submission(
    client: AsyncClient, request_row: PriorAuthRequest, recorded_audit: RecordedAudit
) -> None:
    await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={"submission_method": "covermymeds", "outcome": "complete"},
    )

    assert recorded_audit.actions == [AuditAction.SUBMIT_PRIOR_AUTH]


async def test_routing_returns_what_a_path_is_chosen_from(
    client: AsyncClient, request_row: PriorAuthRequest, encounter: Encounter
) -> None:
    encounter.launch_id = "launch-2f9c"

    response = await client.get(f"/prior-auth/{request_row.id}/routing")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "request_id": str(request_row.id),
        "status": PRIOR_AUTH_STATUS_PENDING,
        "payer_name": "Aetna",
        "launch_id": "launch-2f9c",
        "submittable": True,
    }


async def test_routing_carries_no_clinical_content(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """The reason this route exists at all, asserted rather than assumed.

    A router chooses a path from the payer and the launch; it never looks at a
    diagnosis or a note excerpt. Pulling those across the network for a decision
    that ignores them is what the narrow payload avoids — and what makes the
    absent audit row correct rather than an omission.
    """
    data = (await client.get(f"/prior-auth/{request_row.id}/routing")).json()["data"]

    assert not {
        "clinical_evidence",
        "diagnoses",
        "procedures",
        "patient_fhir_id",
        "insurance_member_id",
    } & set(data)


async def test_routing_writes_no_audit_row(
    client: AsyncClient, request_row: PriorAuthRequest, recorded_audit: RecordedAudit
) -> None:
    """CLAUDE.md's audit rule is an "if and only if", in both directions.

    The audit table's value comes from every row in it being a PHI access, so a
    read over non-clinical data must not write one. The full read next door does
    audit, correctly, because it returns clinical evidence.
    """
    await client.get(f"/prior-auth/{request_row.id}/routing")

    assert recorded_audit.calls == []


async def test_routing_reports_a_denied_request_as_submittable(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """The flag exists so no caller re-derives the rule from ``submitted_at``.

    A denied request carries one and is still submittable; a caller reasoning
    from the timestamp alone would refuse TASK-072's whole flow.
    """
    request_row.submitted_at = datetime.datetime.now(datetime.UTC)
    request_row.status = PRIOR_AUTH_STATUS_DENIED

    data = (await client.get(f"/prior-auth/{request_row.id}/routing")).json()["data"]

    assert data["submittable"] is True


async def test_routing_reports_a_live_request_as_not_submittable(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """A request the payer is still holding must not be sent again."""
    request_row.submitted_at = datetime.datetime.now(datetime.UTC)
    request_row.status = PRIOR_AUTH_STATUS_SUBMITTED

    data = (await client.get(f"/prior-auth/{request_row.id}/routing")).json()["data"]

    assert data["submittable"] is False


async def test_routing_reports_a_missing_launch_as_null(
    client: AsyncClient, request_row: PriorAuthRequest, encounter: Encounter
) -> None:
    """No launch means no credential, so no automated path — not an error here."""
    encounter.launch_id = None

    data = (await client.get(f"/prior-auth/{request_row.id}/routing")).json()["data"]

    assert data["launch_id"] is None


async def test_routing_is_404_for_an_unknown_request() -> None:
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: PriorAuthSession(
        encounter=None, request=None
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://track-a-clinical"
    ) as http:
        response = await http.get(f"/prior-auth/{uuid.uuid4()}/routing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ERROR_CODE_REQUEST_NOT_FOUND


async def test_a_second_submission_is_refused(
    client: AsyncClient, request_row: PriorAuthRequest, fake: PriorAuthSession
) -> None:
    body = {
        "submission_method": "fhir-pas",
        "outcome": "complete",
        "payer_reference_number": PAYER_REFERENCE,
    }
    first = await client.patch(f"/prior-auth/{request_row.id}/submission", json=body)
    second = await client.patch(f"/prior-auth/{request_row.id}/submission", json=body)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == ERROR_CODE_ALREADY_SUBMITTED
    # Both attempts reached the update: the refusal is the WHERE clause's answer
    # rather than a read taken before it.
    assert fake.updates == 2


async def test_the_first_submission_writes_attempt_one(
    client: AsyncClient, request_row: PriorAuthRequest, fake: PriorAuthSession
) -> None:
    """The history half of the write, which the parent's columns cannot show."""
    await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={
            "submission_method": "fhir-pas",
            "outcome": "complete",
            "payer_reference_number": PAYER_REFERENCE,
        },
    )

    assert len(fake.added) == 1
    attempt = fake.added[0]
    assert attempt.attempt_number == 1
    assert attempt.submission_method == "fhir-pas"
    assert attempt.payer_outcome == "complete"
    assert attempt.payer_reference_number == PAYER_REFERENCE
    assert attempt.request_id == request_row.id


async def test_a_denied_request_may_be_resubmitted_as_a_second_attempt(
    client: AsyncClient, request_row: PriorAuthRequest, fake: PriorAuthSession
) -> None:
    """TASK-072's flow. The capability this task exists to provide.

    Attempt 1 stays exactly as it was: a resubmission is a new fact, never an
    overwrite of what the payer said the first time.
    """
    await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={
            "submission_method": "fhir-pas",
            "outcome": "complete",
            "payer_reference_number": PAYER_REFERENCE,
        },
    )
    # The payer decided against it. Nothing in this repository reads an
    # adjudication yet, so the state is set directly — what is under test is the
    # resubmission, not how the row got to denied.
    request_row.status = PRIOR_AUTH_STATUS_DENIED

    second = await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={
            "submission_method": "covermymeds",
            "outcome": "queued",
            "payer_reference_number": None,
        },
    )

    assert second.status_code == 200
    assert [attempt.attempt_number for attempt in fake.added] == [1, 2]
    first, latest = fake.added
    assert first.payer_outcome == "complete"
    assert first.payer_reference_number == PAYER_REFERENCE
    assert latest.submission_method == "covermymeds"
    assert latest.payer_outcome == "queued"
    assert latest.payer_reference_number is None
    # And the parent carries the latest attempt's result, which is what every
    # existing reader wants.
    assert request_row.payer_outcome == "queued"


async def test_a_request_the_payer_refused_may_be_resubmitted(
    client: AsyncClient, request_row: PriorAuthRequest, fake: PriorAuthSession
) -> None:
    """``error`` means the payer never took it in, so nothing is pending.

    Refusing a resubmission here would strand a request that no payer is
    holding and no person was told to chase.
    """
    first = await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={"submission_method": "fhir-pas", "outcome": "error"},
    )
    assert first.status_code == 200
    assert request_row.status == PRIOR_AUTH_STATUS_ERROR

    second = await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={"submission_method": "fhir-pas", "outcome": "queued"},
    )

    assert second.status_code == 200
    assert [attempt.attempt_number for attempt in fake.added] == [1, 2]


async def test_a_refused_resubmission_writes_no_attempt(
    client: AsyncClient, request_row: PriorAuthRequest, fake: PriorAuthSession
) -> None:
    """A request the payer is still holding must not be asked again.

    The unchanged half of the guard: what TASK-061 widened is which *states* may
    be submitted from, never whether a live request may be sent twice.
    """
    body = {"submission_method": "fhir-pas", "outcome": "complete"}
    await client.patch(f"/prior-auth/{request_row.id}/submission", json=body)
    refused = await client.patch(f"/prior-auth/{request_row.id}/submission", json=body)

    assert refused.status_code == 409
    assert len(fake.added) == 1


async def test_a_refused_second_submission_writes_no_audit_row(
    client: AsyncClient, request_row: PriorAuthRequest, recorded_audit: RecordedAudit
) -> None:
    """A row claiming a submission that did not happen is the same lie in the trail."""
    body = {"submission_method": "fhir-pas", "outcome": "complete"}
    await client.patch(f"/prior-auth/{request_row.id}/submission", json=body)
    await client.patch(f"/prior-auth/{request_row.id}/submission", json=body)

    assert recorded_audit.actions == [AuditAction.SUBMIT_PRIOR_AUTH]


async def test_a_refused_second_submission_rolls_back(
    client: AsyncClient, request_row: PriorAuthRequest, fake: PriorAuthSession
) -> None:
    body = {"submission_method": "fhir-pas", "outcome": "complete"}
    await client.patch(f"/prior-auth/{request_row.id}/submission", json=body)
    await client.patch(f"/prior-auth/{request_row.id}/submission", json=body)

    assert fake.rollbacks == 1


async def test_an_unknown_submission_method_is_rejected_by_the_contract(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """The closed vocabulary is enforced at the boundary, not only at the column."""
    response = await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={"submission_method": "FHIR_PAS", "outcome": "complete"},
    )

    assert response.status_code == 422


async def test_an_unknown_outcome_is_rejected_by_the_contract(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    response = await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={"submission_method": "fhir-pas", "outcome": "approved"},
    )

    assert response.status_code == 422


async def test_a_submission_without_an_outcome_is_rejected(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """Every path has an answer, and one recorded without it reads as pending."""
    response = await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={"submission_method": "fhir-pas"},
    )

    assert response.status_code == 422


async def test_unknown_body_fields_are_refused(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    response = await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={"submission_method": "fhir-pas", "outcome": "complete", "status": "approved"},
    )

    assert response.status_code == 422


async def test_the_recorded_time_is_the_servers(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """``submitted_at`` is written server-side; no caller supplies it."""
    before = datetime.datetime.now(datetime.UTC)

    await client.patch(
        f"/prior-auth/{request_row.id}/submission",
        json={"submission_method": "fhir-pas", "outcome": "complete"},
    )

    assert request_row.submitted_at is not None
    assert request_row.submitted_at >= before


class ListSession:
    """A session whose ``execute`` answers a list query with the rows it was given.

    The list route's own SQL — the provider filter, the ordering and the cursor's
    row-wise comparison — is not what this fake can prove, and the integration
    suite next door is where those are asserted against a real PostgreSQL. What
    this one is for is the part a database cannot show: which fields leave the
    service, and that no audit row is written.
    """

    def __init__(self, rows: list[tuple[PriorAuthRequest, Encounter]]) -> None:
        self.rows = rows
        self.commits = 0

    async def execute(self, _statement: Any) -> Any:
        return _ListResult(self.rows)

    async def commit(self) -> None:
        self.commits += 1


class _ListResult:
    def __init__(self, rows: list[tuple[PriorAuthRequest, Encounter]]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return [_Row(row) for row in self._rows]


def denied(request: PriorAuthRequest) -> PriorAuthRequest:
    """Put a request in the state a provider would be following up."""
    request.status = PRIOR_AUTH_STATUS_DENIED
    request.submitted_at = datetime.datetime(2026, 3, 1, 9, 30, tzinfo=datetime.UTC)
    request.decided_at = datetime.datetime(2026, 3, 3, 14, 5, tzinfo=datetime.UTC)
    request.payer_outcome = "complete"
    request.payer_reference_number = PAYER_REFERENCE
    request.denial_reason = "Six weeks of conservative therapy not documented."
    return request


@pytest_asyncio.fixture
async def list_client(
    encounter: Encounter, request_row: PriorAuthRequest, recorded_audit: RecordedAudit
) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: ListSession([(request_row, encounter)])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://track-a-clinical"
    ) as http:
        yield http


async def test_list_returns_the_providers_queue(
    list_client: AsyncClient,
    request_row: PriorAuthRequest,
    encounter: Encounter,
) -> None:
    response = await list_client.get(f"/prior-auth?provider_id={encounter.provider_id}")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["next_cursor"] is None
    [row] = payload["requests"]
    assert row["request_id"] == str(request_row.id)
    assert row["session_id"] == str(encounter.session_id)
    assert row["status"] == PRIOR_AUTH_STATUS_PENDING
    assert row["submittable"] is True


async def test_list_carries_no_clinical_field(
    list_client: AsyncClient, request_row: PriorAuthRequest, encounter: Encounter
) -> None:
    """The guard on the constraint that keeps this route out of ``audit_log``.

    A clinical field added to the row would make the route's silence wrong, and
    this is what fails when someone adds one. ``denial_reason`` is checked on a
    *denied* request, so its absence is the payload's doing rather than the
    fixture happening to carry no denial.
    """
    denied(request_row)

    response = await list_client.get(f"/prior-auth?provider_id={encounter.provider_id}")

    [row] = response.json()["data"]["requests"]
    for field in ("denial_reason", "procedures", "diagnoses", "clinical_evidence"):
        assert field not in row
    assert "patient_fhir_id" not in row


async def test_list_writes_no_audit_row(
    list_client: AsyncClient, encounter: Encounter, recorded_audit: RecordedAudit
) -> None:
    """Non-clinical read, so auditing it would dilute the table it would be written to."""
    await list_client.get(f"/prior-auth?provider_id={encounter.provider_id}")

    assert recorded_audit.actions == []


async def test_list_refuses_to_answer_unscoped(list_client: AsyncClient) -> None:
    """A wider read than anything else here, so the scope is required rather than defaulted."""
    response = await list_client.get("/prior-auth")

    assert response.status_code == 422


async def test_list_refuses_a_cursor_it_did_not_issue(
    list_client: AsyncClient, encounter: Encounter
) -> None:
    """Refused rather than ignored: a silent restart reads as a page that ran out of rows."""
    response = await list_client.get(
        f"/prior-auth?provider_id={encounter.provider_id}&cursor=not-a-cursor"
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == ERROR_CODE_INVALID_CURSOR


async def test_list_reports_the_visits_own_date(
    list_client: AsyncClient, encounter: Encounter
) -> None:
    """``prior_auth_requests`` has no timestamp of its own; the visit's start is the row's date.

    Deliberately not the fixture's own date: an assertion against that value
    would hold even if this field were never read from the encounter at all.
    """
    encounter.started_at = datetime.datetime(2025, 11, 3, 16, 45, tzinfo=datetime.UTC)

    response = await list_client.get(f"/prior-auth?provider_id={encounter.provider_id}")

    [row] = response.json()["data"]["requests"]
    assert row["started_at"].startswith("2025-11-03T16:45:00")


async def test_list_reports_a_manual_request_as_its_own_state(
    list_client: AsyncClient, request_row: PriorAuthRequest, encounter: Encounter
) -> None:
    """Work for a person is neither a failure nor a pending payer decision."""
    request_row.status = PRIOR_AUTH_STATUS_MANUAL_REQUIRED

    response = await list_client.get(f"/prior-auth?provider_id={encounter.provider_id}")

    [row] = response.json()["data"]["requests"]
    assert row["status"] == PRIOR_AUTH_STATUS_MANUAL_REQUIRED
    assert row["submission_method"] is None


async def test_decision_returns_the_payers_answer(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    denied(request_row)

    response = await client.get(f"/prior-auth/{request_row.id}/decision")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["denial_reason"] == request_row.denial_reason
    assert data["payer_reference_number"] == PAYER_REFERENCE
    assert data["payer_outcome"] == "complete"


async def test_decision_carries_no_clinical_evidence(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """Narrow on purpose: note excerpts are not read to render one sentence."""
    denied(request_row)

    response = await client.get(f"/prior-auth/{request_row.id}/decision")

    data = response.json()["data"]
    assert "clinical_evidence" not in data
    assert "procedures" not in data


async def test_decision_audits_as_a_prior_auth_read(
    client: AsyncClient, request_row: PriorAuthRequest, recorded_audit: RecordedAudit
) -> None:
    """The contrast with the list beside it: a denial reason is clinical content."""
    denied(request_row)

    await client.get(f"/prior-auth/{request_row.id}/decision")

    assert recorded_audit.actions == [AuditAction.READ_PRIOR_AUTH]


async def test_decision_distinguishes_no_reason_given_from_no_denial(
    client: AsyncClient, request_row: PriorAuthRequest
) -> None:
    """A payer that denied without saying why is not a request that was never denied."""
    denied(request_row)
    request_row.denial_reason = None

    response = await client.get(f"/prior-auth/{request_row.id}/decision")

    data = response.json()["data"]
    assert data["denial_reason"] is None
    assert data["decided_at"] is not None


async def test_decision_404s_for_an_unknown_request(recorded_audit: RecordedAudit) -> None:
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: PriorAuthSession(
        encounter=None, request=None
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://track-a-clinical"
    ) as http:
        response = await http.get(f"/prior-auth/{uuid.uuid4()}/decision")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ERROR_CODE_REQUEST_NOT_FOUND
    assert recorded_audit.actions == []

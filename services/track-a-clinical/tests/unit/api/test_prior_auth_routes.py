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
    ERROR_CODE_REQUEST_NOT_FOUND,
)
from track_a_clinical.main import create_app
from track_a_clinical.models import (
    PRIOR_AUTH_STATUS_DENIED,
    PRIOR_AUTH_STATUS_ERROR,
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

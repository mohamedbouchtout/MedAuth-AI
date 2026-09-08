"""``POST /prior-auth/{request_id}/submit`` — the HTTP surface. TASK-061.

:mod:`tests.unit.test_router` covers the routing model. What is asserted here is
the contract a client depends on: that both outcomes come back as 200 with a
shape that tells them apart, that a request the payer is holding is a 409 rather
than a second submission, and that an upstream failure is reported as the
transient thing it is without ever looking like a completed submission.

Backed by fakes rather than by the real services, like the route tests in the
services this one calls.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from prior_auth import router as routing
from prior_auth import submission_client
from prior_auth.api.dependencies import get_db_session
from prior_auth.api.submission import (
    ERROR_CODE_NOT_SUBMITTABLE,
    ERROR_CODE_REQUEST_NOT_FOUND,
    ERROR_CODE_SUBMISSION_REFUSED,
    ERROR_CODE_UPSTREAM_UNAVAILABLE,
)
from prior_auth.main import create_app

REQUEST_ID = str(uuid.uuid4())
LAUNCH_ID = "launch-2f9c"


class FakeSession:
    """Absorbs the manual-case write."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def facts(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The owning service's answer, mutable per test."""
    state: dict[str, Any] = {
        "payer_name": "Medicare Advantage",
        "launch_id": LAUNCH_ID,
        "submittable": True,
        "status": "pending",
        "error": None,
    }

    async def fake_read(request_id: str, **_kwargs: Any) -> submission_client.RoutingFacts:
        if state["error"] is not None:
            raise state["error"]
        return submission_client.RoutingFacts(
            request_id=request_id,
            status=state["status"],
            payer_name=state["payer_name"],
            launch_id=state["launch_id"],
            submittable=state["submittable"],
        )

    monkeypatch.setattr(submission_client, "read_routing_facts", fake_read)
    return state


@pytest.fixture
def submitter(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The submitting service, answering with an adjudicated request by default."""
    state: dict[str, Any] = {"error": None, "outcome": "complete", "calls": []}

    async def fake_submit(request_id: str, **kwargs: Any) -> submission_client.SubmissionResult:
        state["calls"].append({"request_id": request_id, **kwargs})
        if state["error"] is not None:
            raise state["error"]
        return submission_client.SubmissionResult(
            request_id=request_id,
            outcome=state["outcome"],
            submission_method="fhir-pas",
            payer_reference_number="AUTH-88213",
        )

    monkeypatch.setattr(submission_client, "submit_through_fhir_integration", fake_submit)
    return state


@pytest_asyncio.fixture
async def client(session: FakeSession) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://prior-auth") as http:
        yield http


async def test_a_supported_payer_is_submitted(
    client: AsyncClient, facts: dict[str, Any], submitter: dict[str, Any]
) -> None:
    response = await client.post(f"/prior-auth/{REQUEST_ID}/submit")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["outcome"] == routing.RoutingOutcome.SUBMITTED
    assert data["payer_outcome"] == "complete"
    assert data["submission_method"] == "fhir-pas"
    assert data["payer_reference_number"] == "AUTH-88213"
    assert data["reason"] is None
    assert len(submitter["calls"]) == 1


async def test_an_unsupported_payer_is_flagged_for_a_person(
    client: AsyncClient, facts: dict[str, Any], submitter: dict[str, Any]
) -> None:
    """200 and an outcome, not an error.

    Most commercial plans are outside the mandate. Reporting this as a failure
    would make the ordinary case look broken; reporting it as submitted would
    leave a provider waiting on a payer nobody asked.
    """
    facts["payer_name"] = "Aetna"

    response = await client.post(f"/prior-auth/{REQUEST_ID}/submit")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["outcome"] == routing.RoutingOutcome.MANUAL_REQUIRED
    assert data["reason"] == routing.REASON_PAYER_NOT_SUPPORTED
    assert data["submission_method"] is None
    assert data["payer_outcome"] is None
    assert submitter["calls"] == []


async def test_the_two_outcomes_are_distinguishable_without_a_second_call(
    client: AsyncClient, facts: dict[str, Any], submitter: dict[str, Any]
) -> None:
    """A dashboard has to render three states, so the payload must carry them."""
    submitted = (await client.post(f"/prior-auth/{REQUEST_ID}/submit")).json()["data"]
    facts["payer_name"] = "Cigna"
    manual = (await client.post(f"/prior-auth/{REQUEST_ID}/submit")).json()["data"]

    assert submitted["outcome"] != manual["outcome"]
    assert bool(submitted["submission_method"]) != bool(manual["submission_method"])
    assert bool(submitted["reason"]) != bool(manual["reason"])


async def test_a_request_the_payer_is_holding_is_a_409(
    client: AsyncClient, facts: dict[str, Any], submitter: dict[str, Any]
) -> None:
    """Checked before any work, and the submitter is never called."""
    facts["submittable"] = False
    facts["status"] = "submitted"

    response = await client.post(f"/prior-auth/{REQUEST_ID}/submit")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ERROR_CODE_NOT_SUBMITTABLE
    assert submitter["calls"] == []


async def test_a_denied_request_is_resubmitted(
    client: AsyncClient, facts: dict[str, Any], submitter: dict[str, Any]
) -> None:
    """TASK-072's flow reaches the payer rather than a 409.

    The owning service says whether a request may be sent; this route relays
    that answer instead of re-deriving it from a submission timestamp.
    """
    facts["status"] = "denied"
    facts["submittable"] = True

    response = await client.post(f"/prior-auth/{REQUEST_ID}/submit")

    assert response.status_code == 200
    assert len(submitter["calls"]) == 1


async def test_a_race_lost_at_the_submitter_is_still_a_409(
    client: AsyncClient, facts: dict[str, Any], submitter: dict[str, Any]
) -> None:
    """The pre-check is a courtesy; the owning service's update is the guarantee.

    A caller that passed the check and lost the race must be told it lost, not
    handed a generic failure that invites another attempt.
    """
    submitter["error"] = submission_client.SubmissionRefused(
        submission_client.ERROR_CODE_ALREADY_SUBMITTED, status_code=409
    )

    response = await client.post(f"/prior-auth/{REQUEST_ID}/submit")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ERROR_CODE_NOT_SUBMITTABLE


async def test_an_unconfigured_path_is_reported_as_manual_not_as_a_failure(
    client: AsyncClient, facts: dict[str, Any], submitter: dict[str, Any]
) -> None:
    """The payer takes PAS; this EHR delivers by CoverMyMeds and nothing
    configured it. Not transient, and not something to retry."""
    submitter["error"] = submission_client.SubmissionRefused(
        submission_client.ERROR_CODE_PATH_NOT_CONFIGURED, status_code=422
    )

    response = await client.post(f"/prior-auth/{REQUEST_ID}/submit")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["outcome"] == routing.RoutingOutcome.MANUAL_REQUIRED
    assert data["reason"] == routing.REASON_PATH_NOT_CONFIGURED


async def test_another_refusal_is_a_422(
    client: AsyncClient, facts: dict[str, Any], submitter: dict[str, Any]
) -> None:
    submitter["error"] = submission_client.SubmissionRefused(
        "PRIOR_AUTH_NOT_SUBMITTABLE", status_code=422
    )

    response = await client.post(f"/prior-auth/{REQUEST_ID}/submit")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == ERROR_CODE_SUBMISSION_REFUSED


async def test_an_unknown_request_is_a_404(client: AsyncClient, facts: dict[str, Any]) -> None:
    facts["error"] = submission_client.RoutingReadError("gone", not_found=True)

    response = await client.post(f"/prior-auth/{REQUEST_ID}/submit")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ERROR_CODE_REQUEST_NOT_FOUND


async def test_an_unreachable_clinical_service_is_a_502(
    client: AsyncClient, facts: dict[str, Any], submitter: dict[str, Any]
) -> None:
    """Nothing was transmitted, so retrying is reasonable — and it must not look
    like a request that reached a payer."""
    facts["error"] = submission_client.RoutingReadError("unreachable")

    response = await client.post(f"/prior-auth/{REQUEST_ID}/submit")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == ERROR_CODE_UPSTREAM_UNAVAILABLE
    assert response.json()["data"] is None
    assert submitter["calls"] == []


async def test_a_timed_out_submission_is_a_504_that_warns_against_retrying(
    client: AsyncClient, facts: dict[str, Any], submitter: dict[str, Any]
) -> None:
    """A timeout is genuinely ambiguous: the payer may have taken the request in.

    So the message says to reconcile rather than resubmit. Reporting it as a
    plain failure would invite a retry that opens a second review — the failure
    every guard on this path exists to prevent.
    """
    submitter["error"] = submission_client.SubmissionUnavailable("slow", timed_out=True)

    response = await client.post(f"/prior-auth/{REQUEST_ID}/submit")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == ERROR_CODE_UPSTREAM_UNAVAILABLE
    assert "reconcile" in response.json()["error"]["message"]


async def test_a_bad_request_id_is_a_422(client: AsyncClient) -> None:
    response = await client.post("/prior-auth/not-a-uuid/submit")

    assert response.status_code == 422

"""Populating an encounter's payer columns at session start. TASK-052b.

These three columns plus ``cpt_code`` are what ``POST /policies/query`` is keyed
on, so what they hold decides which payer's policy an encounter is checked
against. Two properties are worth more than the happy path here, and both are
things a later change could erode without any test noticing:

* **A field the EHR did not hold stays NULL.** The Redis cache key is
  ``rag:{payer}:{plan_type}:{state}:{cpt_code}``, so a default filled in for a
  missing segment writes a real policy answer under a key standing for a
  different plan — and serves it to the next encounter that matches.
* **A failed lookup does not fail the session.** A provider unable to record a
  visit because a payer lookup timed out is a worse outcome than a visit whose
  policy queries cannot be built, and the dispatcher already reports the second
  one per procedure.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from tests.unit.api.conftest import FakeSession
from track_a_clinical import coverage_context

START_BODY: dict[str, Any] = {
    "patient_id": "synthea-placeholder-1",
    "provider_id": str(uuid.uuid4()),
    "ehr_encounter_id": "athena-enc-9001",
    "launch_id": "3f2a7c18-0d64-4a51-9f0e-8b1c2d3e4f50",
}

FULL_CONTEXT: dict[str, Any] = {
    "encounter_id": "athena-enc-9001",
    "patient_id": "synthea-placeholder-1",
    "coverage": {
        "payer": "Blue Cross Blue Shield of Massachusetts",
        "plan_type": "PPO",
        "member_id": "MEM-001",
    },
    "state": "MA",
    "requires_manual_confirmation": False,
}


class RecordedFHIRIntegration:
    """Stands in for ``fhir-integration``, recording what was asked of it."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Give the module's client a transport this fake answers.

        The service boundary itself is not stubbed out — the call still goes
        over HTTP through the real client, which is what keeps the audit row
        ``fhir-integration``'s route writes on the only path to a chart read.
        """
        transport = httpx.MockTransport(self.handler)
        original = httpx.AsyncClient

        def build(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(coverage_context.httpx, "AsyncClient", build)


def responding(payload: dict[str, Any] | None, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json={"data": payload, "error": None})


class TestTheColumnsArePopulated:
    async def test_all_three_columns_come_from_the_ehr(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        RecordedFHIRIntegration(responding(FULL_CONTEXT)).install(monkeypatch)

        response = await client.post("/sessions/start", json=START_BODY)

        assert response.status_code == 201
        (encounter,) = fake_session.added
        assert encounter.insurance_payer == "Blue Cross Blue Shield of Massachusetts"
        assert encounter.insurance_plan_type == "PPO"
        assert encounter.insurance_member_id == "MEM-001"
        assert encounter.state == "MA"

    async def test_the_launch_is_recorded_on_the_encounter(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit mapping, never an equality with ``session_id``.

        The two identifiers have different lifetimes and one launch can outlive
        several encounters, so which launch a visit came from has to be recorded
        rather than inferred. See CLAUDE.md, "A SMART launch is not an encounter
        session".
        """
        RecordedFHIRIntegration(responding(FULL_CONTEXT)).install(monkeypatch)

        body = (await client.post("/sessions/start", json=START_BODY)).json()

        (encounter,) = fake_session.added
        assert encounter.launch_id == START_BODY["launch_id"]
        assert encounter.launch_id != body["data"]["session_id"]

    async def test_the_payer_is_stored_as_the_ehr_spelled_it(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``/policies/query`` is the single site that slugs a payer.

        The column is documented as the payer's own spelling, and a slug written
        here would put one normalisation rule in two places — the drift this
        repository already fixed once for the retrieval filter.
        """
        RecordedFHIRIntegration(responding(FULL_CONTEXT)).install(monkeypatch)

        await client.post("/sessions/start", json=START_BODY)

        (encounter,) = fake_session.added
        assert encounter.insurance_payer == "Blue Cross Blue Shield of Massachusetts"

    async def test_the_launch_id_travels_in_a_header_not_the_url(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It resolves to an EHR access token, so it is a credential handle.

        A query string is the one place a credential is certain to be logged by
        intermediaries, and a path segment lands in the same access logs.
        """
        fake = RecordedFHIRIntegration(responding(FULL_CONTEXT))
        fake.install(monkeypatch)

        await client.post("/sessions/start", json=START_BODY)

        (request,) = fake.requests
        assert request.headers[coverage_context.LAUNCH_ID_HEADER] == START_BODY["launch_id"]
        assert START_BODY["launch_id"] not in str(request.url)
        assert request.url.path.endswith("/fhir/encounter/athena-enc-9001/coverage-context")


class TestAMissingFieldStaysNull:
    async def test_a_coverage_with_no_plan_type_leaves_that_column_null(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Never a default guessed from the payer's name.

        ``resolve_query_parameters()`` then names ``plan_type`` alone as missing,
        which is a more useful answer than a plausible wrong one.
        """
        partial = {
            **FULL_CONTEXT,
            "coverage": {"payer": "Aetna", "plan_type": None, "member_id": "MEM-002"},
            "requires_manual_confirmation": True,
        }
        RecordedFHIRIntegration(responding(partial)).install(monkeypatch)

        await client.post("/sessions/start", json=START_BODY)

        (encounter,) = fake_session.added
        assert encounter.insurance_payer == "Aetna"
        assert encounter.insurance_plan_type is None
        assert encounter.state == "MA"

    async def test_no_coverage_leaves_the_payer_columns_null_and_keeps_the_state(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Synthea outcome: no searchable ``Coverage`` exists, and a state does.

        Asserted so the blocker cannot later be "fixed" by digging the contained
        ``#coverage`` out of an ``ExplanationOfBenefit`` — Synthea sets its
        ``type`` to the payer's name rather than a plan type, so it would supply
        a wrong ``plan_type`` rather than a missing one.
        """
        no_coverage = {**FULL_CONTEXT, "coverage": None, "requires_manual_confirmation": True}
        RecordedFHIRIntegration(responding(no_coverage)).install(monkeypatch)

        await client.post("/sessions/start", json=START_BODY)

        (encounter,) = fake_session.added
        assert encounter.insurance_payer is None
        assert encounter.insurance_plan_type is None
        assert encounter.state == "MA"

    async def test_an_encounter_with_no_site_of_care_leaves_the_state_null(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No fallback to the patient's residence, at this layer either."""
        placeless = {**FULL_CONTEXT, "state": None}
        RecordedFHIRIntegration(responding(placeless)).install(monkeypatch)

        await client.post("/sessions/start", json=START_BODY)

        (encounter,) = fake_session.added
        assert encounter.state is None
        assert encounter.insurance_payer == "Blue Cross Blue Shield of Massachusetts"


class TestTheLookupNeverFailsTheSession:
    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(502, json={"data": None, "error": {"code": "x", "message": "y"}}),
            httpx.Response(404, json={"data": None, "error": {"code": "x", "message": "y"}}),
            httpx.Response(200, json={"not": "the envelope"}),
        ],
        ids=["upstream-error", "unknown-launch", "unusable-body"],
    )
    async def test_a_failed_lookup_still_starts_the_session(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
        response: httpx.Response,
    ) -> None:
        """A provider unable to record a visit is the worse failure.

        The columns stay NULL and the dispatcher reports them missing per
        procedure, so nothing is hidden — it is reported one layer down.
        """
        RecordedFHIRIntegration(response).install(monkeypatch)

        result = await client.post("/sessions/start", json=START_BODY)

        assert result.status_code == 201
        (encounter,) = fake_session.added
        assert encounter.insurance_payer is None
        assert encounter.state is None

    async def test_a_transport_failure_still_starts_the_session(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("fhir-integration is down", request=request)

        transport = httpx.MockTransport(explode)
        original = httpx.AsyncClient
        monkeypatch.setattr(
            coverage_context.httpx,
            "AsyncClient",
            lambda *a, **kw: original(*a, **{**kw, "transport": transport}),
        )

        result = await client.post("/sessions/start", json=START_BODY)

        assert result.status_code == 201
        (encounter,) = fake_session.added
        assert encounter.state is None


class TestWhenTheLookupIsNotAttempted:
    async def test_a_session_started_without_a_launch_makes_no_call(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not every visit comes from a SMART launch, and that is not an error."""
        fake = RecordedFHIRIntegration(responding(FULL_CONTEXT))
        fake.install(monkeypatch)
        body = {key: value for key, value in START_BODY.items() if key != "launch_id"}

        result = await client.post("/sessions/start", json=body)

        assert result.status_code == 201
        assert fake.requests == []
        (encounter,) = fake_session.added
        assert encounter.launch_id is None
        assert encounter.state is None

    async def test_a_launch_with_no_ehr_encounter_id_makes_no_call(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The route is keyed on the EHR's encounter; there is nothing to ask about.

        The launch is still recorded, so a later task can complete the lookup
        once an encounter id is known.
        """
        fake = RecordedFHIRIntegration(responding(FULL_CONTEXT))
        fake.install(monkeypatch)
        body = {key: value for key, value in START_BODY.items() if key != "ehr_encounter_id"}

        result = await client.post("/sessions/start", json=body)

        assert result.status_code == 201
        assert fake.requests == []
        (encounter,) = fake_session.added
        assert encounter.launch_id == START_BODY["launch_id"]

    async def test_a_session_id_sent_as_a_launch_id_is_not_special_cased(
        self,
        client: AsyncClient,
        fake_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """It is sent on as given and answered with a 404 by the other service.

        There is deliberately no fallback that tries a value as the other kind
        of identifier — that is how one name for two things gets established.
        """
        fake = RecordedFHIRIntegration(
            httpx.Response(404, json={"data": None, "error": {"code": "x", "message": "y"}})
        )
        fake.install(monkeypatch)
        mistaken = str(uuid.uuid4())

        result = await client.post("/sessions/start", json={**START_BODY, "launch_id": mistaken})

        assert result.status_code == 201
        (request,) = fake.requests
        assert request.headers[coverage_context.LAUNCH_ID_HEADER] == mistaken
        (encounter,) = fake_session.added
        assert encounter.insurance_payer is None

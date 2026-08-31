"""The base adapter's primitives and its two composed methods. TASK-052, TASK-052b.

Two things get most of the attention here, because they are the two the task
settled explicitly and the two a later change is most likely to erode:

* **The coverage rule**, row by row from TASK-052's table. It gates TASK-052b's
  decision to leave an encounter column NULL, so an imprecision here becomes an
  imprecision in what a payer policy query is asked about.
* **The three FHIR failure outcomes staying three.** Collapsing them would let
  an outage read as a patient with no insurance.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from src.adapters.base import EHRAdapter, needs_manual_confirmation
from src.adapters.errors import (
    FHIRAuthorizationExpired,
    FHIRMalformedResponse,
    FHIRResourceNotFound,
    FHIRUpstreamUnavailable,
)
from tests.unit.conftest import (
    ACCESS_TOKEN,
    FHIR_BASE_URL,
    FakeFHIRServer,
    condition_resource,
    coverage_resource,
    encounter_resource,
    location_resource,
    organization_resource,
    patient_resource,
    search_bundle,
)


def adapter_for(ehr: FakeFHIRServer) -> EHRAdapter:
    return EHRAdapter(
        fhir_base_url=FHIR_BASE_URL, access_token=ACCESS_TOKEN, http_client=ehr.client()
    )


@pytest.fixture
def ehr() -> FakeFHIRServer:
    return FakeFHIRServer()


class TestPrimitives:
    async def test_the_patient_is_flattened_out_of_the_resource(self, ehr: FakeFHIRServer) -> None:
        patient = await adapter_for(ehr).get_patient("synthea-123")

        assert patient.patient_id == "synthea-123"
        assert patient.family_name == "Sanchez"
        assert patient.given_names == ["Aurelio", "Luis"]
        assert patient.birth_date == "1962-04-17"
        assert patient.gender == "male"

    async def test_the_token_travels_on_every_request_not_on_the_client(
        self, ehr: FakeFHIRServer
    ) -> None:
        """The shared client also talks to hosts that must not see this token."""
        await adapter_for(ehr).get_patient("synthea-123")

        assert ehr.authorization_headers == [f"Bearer {ACCESS_TOKEN}"]

    async def test_the_encounter_comes_back_as_the_r4_resource(self, ehr: FakeFHIRServer) -> None:
        encounter = await adapter_for(ehr).get_encounter("encounter-1")

        assert encounter.id == "encounter-1"
        assert encounter.status == "finished"

    async def test_active_conditions_include_recurrence_and_relapse(
        self, ehr: FakeFHIRServer
    ) -> None:
        """US Core's notion of active, not a literal ``active`` match.

        A relapsed problem is an active one, and a payer's criteria may turn on
        exactly that.
        """
        ehr.conditions = [
            condition_resource(condition_id="a", clinical_status="active"),
            condition_resource(condition_id="b", clinical_status="recurrence"),
            condition_resource(condition_id="c", clinical_status="relapse"),
            condition_resource(condition_id="d", clinical_status="resolved"),
            condition_resource(condition_id="e", clinical_status="inactive"),
        ]

        conditions = await adapter_for(ehr).get_conditions("synthea-123")

        assert [condition.id for condition in conditions] == ["a", "b", "c"]

    async def test_a_condition_with_no_clinical_status_is_kept(self, ehr: FakeFHIRServer) -> None:
        """It is not assertably resolved, and dropping it would hide a problem."""
        ehr.conditions = [condition_resource(clinical_status=None)]

        assert len(await adapter_for(ehr).get_conditions("synthea-123")) == 1

    async def test_a_patient_with_no_conditions_gets_an_empty_list_not_an_error(
        self, ehr: FakeFHIRServer
    ) -> None:
        """An empty search Bundle is a successful answer, never a not-found."""
        ehr.conditions = []

        assert await adapter_for(ehr).get_conditions("synthea-123") == []


class TestTheCoverageRule:
    """TASK-052's enumerated table, one test per row."""

    async def test_no_coverage_at_all_is_none_and_needs_confirmation(
        self, ehr: FakeFHIRServer
    ) -> None:
        ehr.coverages = []

        coverage = await adapter_for(ehr).get_coverage("synthea-123")

        assert coverage is None
        assert needs_manual_confirmation(coverage) is True

    async def test_no_active_coverage_is_none_and_needs_confirmation(
        self, ehr: FakeFHIRServer
    ) -> None:
        ehr.coverages = [coverage_resource(status="cancelled")]

        coverage = await adapter_for(ehr).get_coverage("synthea-123")

        assert coverage is None
        assert needs_manual_confirmation(coverage) is True

    async def test_a_missing_payer_still_answers_but_needs_confirmation(
        self, ehr: FakeFHIRServer
    ) -> None:
        ehr.coverages = [coverage_resource(payer_display=None)]

        coverage = await adapter_for(ehr).get_coverage("synthea-123")

        assert coverage is not None
        assert coverage.payer is None
        assert needs_manual_confirmation(coverage) is True

    async def test_a_missing_plan_type_still_answers_but_needs_confirmation(
        self, ehr: FakeFHIRServer
    ) -> None:
        ehr.coverages = [coverage_resource(plan_type_text=None)]

        coverage = await adapter_for(ehr).get_coverage("synthea-123")

        assert coverage is not None
        assert coverage.payer == "Aetna Better Health of MA"
        assert coverage.plan_type is None
        assert needs_manual_confirmation(coverage) is True

    async def test_a_missing_member_id_does_not_need_confirmation(
        self, ehr: FakeFHIRServer
    ) -> None:
        """Deliberate, and the row most likely to be "fixed" by mistake.

        ``member_id`` is not a segment of the rag: cache key and changes no
        policy answer. TASK-060's bundle is what needs it and is far enough
        downstream to check for itself; asking a provider to confirm the payer
        in order to fill it would be noise.
        """
        ehr.coverages = [coverage_resource(subscriber_id=None)]

        coverage = await adapter_for(ehr).get_coverage("synthea-123")

        assert coverage is not None
        assert coverage.member_id is None
        assert needs_manual_confirmation(coverage) is False

    async def test_a_complete_coverage_needs_no_confirmation(self, ehr: FakeFHIRServer) -> None:
        coverage = await adapter_for(ehr).get_coverage("synthea-123")

        assert coverage is not None
        assert coverage.payer == "Aetna Better Health of MA"
        assert coverage.plan_type == "PPO"
        assert coverage.member_id == "W123456789"
        assert needs_manual_confirmation(coverage) is False

    async def test_the_plan_type_falls_back_to_the_plan_class(self, ehr: FakeFHIRServer) -> None:
        """Several EHRs carry it only there, and both places are standard."""
        ehr.coverages = [coverage_resource(plan_type_text=None, plan_class="Gold PPO")]

        coverage = await adapter_for(ehr).get_coverage("synthea-123")

        assert coverage is not None
        assert coverage.plan_type == "Gold PPO"

    async def test_the_lowest_order_wins_when_it_is_unique(self, ehr: FakeFHIRServer) -> None:
        ehr.coverages = [
            coverage_resource(coverage_id="secondary", payer_display="Secondary Payer", order=2),
            coverage_resource(coverage_id="primary", payer_display="Primary Payer", order=1),
        ]

        coverage = await adapter_for(ehr).get_coverage("synthea-123")

        assert coverage is not None
        assert coverage.payer == "Primary Payer"

    async def test_a_tie_on_order_yields_no_coverage_rather_than_a_guess(
        self, ehr: FakeFHIRServer
    ) -> None:
        """The row this table exists for.

        Picking either arbitrarily would answer a policy query against a payer
        the patient may not be primary with — a confident wrong answer, where
        this repository's standing preference is an empty answer plus a visible
        signal.
        """
        ehr.coverages = [
            coverage_resource(coverage_id="a", payer_display="Payer A", order=1),
            coverage_resource(coverage_id="b", payer_display="Payer B", order=1),
        ]

        coverage = await adapter_for(ehr).get_coverage("synthea-123")

        assert coverage is None
        assert needs_manual_confirmation(coverage) is True

    async def test_several_active_coverages_with_no_order_yield_no_coverage(
        self, ehr: FakeFHIRServer
    ) -> None:
        ehr.coverages = [
            coverage_resource(coverage_id="a", payer_display="Payer A"),
            coverage_resource(coverage_id="b", payer_display="Payer B"),
        ]

        assert await adapter_for(ehr).get_coverage("synthea-123") is None

    async def test_the_payer_is_kept_as_the_resource_spelled_it(self, ehr: FakeFHIRServer) -> None:
        """Never slugged here — normalize_payer() is /policies/query's job."""
        ehr.coverages = [coverage_resource(payer_display="Medicare Part B")]

        coverage = await adapter_for(ehr).get_coverage("synthea-123")

        assert coverage is not None
        assert coverage.payer == "Medicare Part B"


class TestTheThreeFailureOutcomes:
    async def test_a_404_is_not_found(self, ehr: FakeFHIRServer) -> None:
        ehr.patient = None

        with pytest.raises(FHIRResourceNotFound):
            await adapter_for(ehr).get_patient("nobody")

    async def test_a_200_operation_outcome_saying_not_found_is_not_found(
        self, ehr: FakeFHIRServer
    ) -> None:
        """Some servers answer a missing resource this way rather than with 404."""
        ehr.fail(
            "/Patient/",
            httpx.Response(
                200,
                json={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "not-found"}],
                },
            ),
        )

        with pytest.raises(FHIRResourceNotFound):
            await adapter_for(ehr).get_patient("nobody")

    async def test_a_5xx_is_unreachable_and_not_a_timeout(self, ehr: FakeFHIRServer) -> None:
        ehr.fail("/Patient/", httpx.Response(503))

        with pytest.raises(FHIRUpstreamUnavailable) as caught:
            await adapter_for(ehr).get_patient("synthea-123")

        assert caught.value.timed_out is False

    async def test_a_timeout_is_unreachable_and_marked_as_one(self, ehr: FakeFHIRServer) -> None:
        """The route turns this into a 504 rather than a 502."""
        ehr.fail("/Patient/", httpx.ReadTimeout("timed out"))

        with pytest.raises(FHIRUpstreamUnavailable) as caught:
            await adapter_for(ehr).get_patient("synthea-123")

        assert caught.value.timed_out is True

    async def test_a_connect_error_is_unreachable(self, ehr: FakeFHIRServer) -> None:
        ehr.fail("/Patient/", httpx.ConnectError("no route to host"))

        with pytest.raises(FHIRUpstreamUnavailable):
            await adapter_for(ehr).get_patient("synthea-123")

    async def test_a_non_json_body_is_malformed(self, ehr: FakeFHIRServer) -> None:
        ehr.fail("/Patient/", httpx.Response(200, text="<html>a login page</html>"))

        with pytest.raises(FHIRMalformedResponse):
            await adapter_for(ehr).get_patient("synthea-123")

    async def test_the_wrong_resource_type_is_malformed(self, ehr: FakeFHIRServer) -> None:
        ehr.fail("/Patient/", httpx.Response(200, json={"resourceType": "Practitioner"}))

        with pytest.raises(FHIRMalformedResponse):
            await adapter_for(ehr).get_patient("synthea-123")

    async def test_a_search_that_does_not_return_a_bundle_is_malformed(
        self, ehr: FakeFHIRServer
    ) -> None:
        ehr.fail("/Coverage", httpx.Response(200, json={"resourceType": "Coverage"}))

        with pytest.raises(FHIRMalformedResponse):
            await adapter_for(ehr).get_coverage("synthea-123")

    async def test_a_401_is_the_expired_launch_outcome(self, ehr: FakeFHIRServer) -> None:
        """Its own outcome, and the seam TASK-051b fills."""
        ehr.fail("/Patient/", httpx.Response(401))

        with pytest.raises(FHIRAuthorizationExpired):
            await adapter_for(ehr).get_patient("synthea-123")

    async def test_a_403_is_the_expired_launch_outcome_too(self, ehr: FakeFHIRServer) -> None:
        ehr.fail("/Patient/", httpx.Response(403))

        with pytest.raises(FHIRAuthorizationExpired):
            await adapter_for(ehr).get_patient("synthea-123")

    async def test_no_response_body_reaches_the_exception_message(
        self, ehr: FakeFHIRServer
    ) -> None:
        """An OperationOutcome's diagnostics is free text written about a chart."""
        ehr.fail(
            "/Patient/",
            httpx.Response(
                404,
                json={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "not-found",
                            "diagnostics": "No patient named Aurelio Sanchez born 1962-04-17",
                        }
                    ],
                },
            ),
        )

        with pytest.raises(FHIRResourceNotFound) as caught:
            await adapter_for(ehr).get_patient("synthea-123")

        assert "Aurelio" not in str(caught.value)
        assert "1962" not in str(caught.value)


class TestTheComposedContext:
    async def test_it_assembles_all_three_primitives(self, ehr: FakeFHIRServer) -> None:
        context = await adapter_for(ehr).get_patient_context("synthea-123")

        assert context.patient.family_name == "Sanchez"
        assert context.coverage is not None
        assert context.coverage.payer == "Aetna Better Health of MA"
        assert len(context.conditions) == 1
        assert context.requires_manual_confirmation is False

    async def test_incomplete_coverage_sets_the_flag_rather_than_failing(
        self, ehr: FakeFHIRServer
    ) -> None:
        """A provider filling the payer in is a working encounter.

        A guessed one is a cache key standing for a plan the patient is not on.
        """
        ehr.coverages = []

        context = await adapter_for(ehr).get_patient_context("synthea-123")

        assert context.coverage is None
        assert context.requires_manual_confirmation is True
        assert context.patient.family_name == "Sanchez"

    async def test_a_failure_in_any_primitive_propagates(self, ehr: FakeFHIRServer) -> None:
        """Not swallowed into a partial context that looks complete."""
        ehr.fail("/Condition", httpx.Response(503))

        with pytest.raises(FHIRUpstreamUnavailable):
            await adapter_for(ehr).get_patient_context("synthea-123")

    async def test_the_searches_are_scoped_to_the_patient(self, ehr: FakeFHIRServer) -> None:
        await adapter_for(ehr).get_patient_context("synthea-123")

        searches = [url for url in ehr.requested_paths if "patient=" in url]
        assert len(searches) == 2
        assert all("patient=synthea-123" in url for url in searches)


class TestTheEmptyBundleIsNotANotFound:
    async def test_no_coverage_on_file_and_no_such_patient_are_different_answers(
        self, ehr: FakeFHIRServer
    ) -> None:
        """Merging them would make "uninsured" and "unknown patient" one outcome."""
        ehr.coverages = []
        assert await adapter_for(ehr).get_coverage("synthea-123") is None

        ehr.fail("/Coverage", httpx.Response(404))
        with pytest.raises(FHIRResourceNotFound):
            await adapter_for(ehr).get_coverage("synthea-123")

    async def test_an_empty_entry_list_is_tolerated(self, ehr: FakeFHIRServer) -> None:
        ehr.fail("/Coverage", httpx.Response(200, json=search_bundle()))

        assert await adapter_for(ehr).get_coverage("synthea-123") is None


class TestTheEncounterCoverageContext:
    """TASK-052b's composed method — the three ``encounters`` columns in one call."""

    async def test_it_assembles_the_payer_half_and_the_site_of_care(
        self, ehr: FakeFHIRServer
    ) -> None:
        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.encounter_id == "encounter-1"
        assert context.patient_id == "synthea-123"
        assert context.coverage is not None
        assert context.coverage.payer == "Aetna Better Health of MA"
        assert context.coverage.plan_type == "PPO"
        assert context.state == "MA"
        assert context.requires_manual_confirmation is False

    async def test_the_patient_is_read_off_the_encounter_not_taken_as_a_parameter(
        self, ehr: FakeFHIRServer
    ) -> None:
        """So a caller cannot pair one encounter with another patient's coverage."""
        ehr.encounter = encounter_resource(subject="Patient/someone-else")
        ehr.patient = patient_resource("someone-else")

        await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert any("Patient/someone-else" in url for url in ehr.requested_paths)
        assert all("patient=synthea-123" not in url for url in ehr.requested_paths)

    async def test_the_payer_is_kept_as_the_resource_spelled_it(self, ehr: FakeFHIRServer) -> None:
        """``/policies/query`` is the single normalisation site; slugging twice is drift.

        The column is documented as the payer's own spelling, so a slug arriving
        here would make the column disagree with its own schema comment.
        """
        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.coverage is not None
        assert context.coverage.payer == "Aetna Better Health of MA"

    async def test_a_coverage_with_no_plan_type_leaves_it_none(self, ehr: FakeFHIRServer) -> None:
        """Never a default guessed from the payer's name — a fabricated key segment."""
        ehr.coverages = [coverage_resource(plan_type_text=None)]

        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.coverage is not None
        assert context.coverage.payer == "Aetna Better Health of MA"
        assert context.coverage.plan_type is None
        assert context.requires_manual_confirmation is True

    async def test_no_coverage_leaves_the_payer_half_empty_but_still_answers_the_state(
        self, ehr: FakeFHIRServer
    ) -> None:
        """The Synthea case: no searchable ``Coverage`` exists, and the state still does.

        Synthea contains the ``Coverage`` inside an ``ExplanationOfBenefit``
        rather than storing it as its own resource, so ``Coverage?patient=``
        comes back empty. That is the honest NULL outcome, asserted here so
        nobody later "fixes" it by digging the contained resource out of an EOB
        — its ``type`` is the payer's name, not a plan type, so it would not
        help anyway.
        """
        ehr.coverages = []

        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.coverage is None
        assert context.requires_manual_confirmation is True
        assert context.state == "MA"


class TestWhereTheStateComesFrom:
    async def test_the_location_wins_over_the_service_provider(self, ehr: FakeFHIRServer) -> None:
        """A Location is the room; an Organization can span states, so it is coarser."""
        ehr.locations = {"loc-1": location_resource(state="NH")}
        ehr.organizations = {"org-1": organization_resource(addresses=[{"state": "MA"}])}

        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.state == "NH"

    async def test_the_service_provider_answers_when_no_location_does(
        self, ehr: FakeFHIRServer
    ) -> None:
        ehr.encounter = encounter_resource(locations=[])
        ehr.organizations = {"org-1": organization_resource(addresses=[{"state": "NH"}])}

        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.state == "NH"

    async def test_a_location_with_no_address_falls_through_to_the_next_location(
        self, ehr: FakeFHIRServer
    ) -> None:
        """A better answer than falling all the way through to the organization."""
        ehr.encounter = encounter_resource(
            locations=[
                {"location": {"reference": "Location/no-address"}},
                {"location": {"reference": "Location/loc-2"}},
            ]
        )
        ehr.locations = {
            "no-address": location_resource("no-address", state=None),
            "loc-2": location_resource("loc-2", state="NH"),
        }
        ehr.organizations = {"org-1": organization_resource(addresses=[{"state": "MA"}])}

        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.state == "NH"

    async def test_a_dangling_location_reference_falls_through_rather_than_failing(
        self, ehr: FakeFHIRServer
    ) -> None:
        """A coarser answer beats no answer; a SMART launch must not fail over this."""
        ehr.locations = {}
        ehr.organizations = {"org-1": organization_resource(addresses=[{"state": "NH"}])}

        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.state == "NH"

    async def test_an_outage_reading_a_location_still_propagates(self, ehr: FakeFHIRServer) -> None:
        """An EHR that was down must not read as an encounter with no location."""
        ehr.fail("/Location/", httpx.Response(503))

        with pytest.raises(FHIRUpstreamUnavailable):
            await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

    async def test_the_patients_address_is_never_a_fallback(self, ehr: FakeFHIRServer) -> None:
        """The guard on the whole rule. NULL is the honest answer; a residence is not.

        This is the obvious "fix" for a NULL state and it produces a plausible
        value nearly every time, which is exactly what makes it dangerous: the
        value keys a cache, so a wrong state serves one plan's answer to a
        patient on another. The policy documents say the site of care, and a
        residence is a different fact that merely coincides most of the time.
        """
        ehr.encounter = encounter_resource(locations=[], service_provider=None)
        ehr.patient = patient_resource(address_state="NH")

        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.state is None

    async def test_a_disagreement_with_the_patients_address_is_logged(
        self, ehr: FakeFHIRServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The site-of-care answer still goes out; the disagreement becomes visible.

        The commercial payers in the corpus state no geographic rule at all, so
        the site-of-care rule is carried over for them rather than documented.
        This is what makes that carry-over auditable instead of invisible.
        """
        ehr.locations = {"loc-1": location_resource(state="NH")}
        ehr.patient = patient_resource(address_state="MA")

        with caplog.at_level(logging.WARNING):
            context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.state == "NH"
        assert "different states" in caplog.text

    async def test_a_cms_sub_state_code_is_normalised_before_it_is_stored(
        self, ehr: FakeFHIRServer
    ) -> None:
        """``CHAR(2)`` could not hold ``CNMI``, and ``QN`` would match no policy row."""
        ehr.locations = {"loc-1": location_resource(state="QN")}

        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.state == "NY"

    async def test_an_encounter_with_no_readable_subject_still_answers_the_state(
        self, ehr: FakeFHIRServer
    ) -> None:
        """The state belongs to the encounter, so it survives an unreadable patient."""
        ehr.encounter = encounter_resource(subject=None)

        context = await adapter_for(ehr).get_encounter_coverage_context("encounter-1")

        assert context.patient_id is None
        assert context.coverage is None
        assert context.requires_manual_confirmation is True
        assert context.state == "MA"

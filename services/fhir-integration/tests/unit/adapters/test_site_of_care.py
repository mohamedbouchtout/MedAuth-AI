"""Where an encounter took place — the ``state`` segment of the cache key. TASK-052b.

The site-of-care rule was not chosen on architectural taste; it is what the real
policy documents say, and the module under test carries the citations. What these
tests protect is the part a later change is most likely to erode by accident:

* **The patient's address is never a fallback.** It is the obvious "fix" for a
  NULL state, it produces a plausible value almost every time, and it is wrong.
  ``test_the_patients_address_is_never_a_fallback`` is the guard.
* **NULL beats a guess.** A cache key is keyed on this value, so a wrong state
  serves one plan's answer to a patient on another — the failure class
  TASK-016/TASK-017 already fixed once for the payer slug.
"""

from __future__ import annotations

import logging

import pytest

from fhir_types import Address, Encounter, Location, Organization, Reference
from src.adapters.site_of_care import (
    location_state,
    log_state_disagreement,
    organization_state,
    patient_address_state,
    reference_id,
    service_provider_reference,
    site_location_references,
    to_usps_state,
)
from tests.unit.conftest import encounter_resource, location_resource, organization_resource


def an_encounter(**kwargs: object) -> Encounter:
    return Encounter.model_validate(encounter_resource(**kwargs))  # type: ignore[arg-type]


class TestReferenceId:
    @pytest.mark.parametrize(
        ("reference", "expected"),
        [
            ("Location/loc-1", "loc-1"),
            ("https://fhir.example.org/r4/Location/loc-1", "loc-1"),
            ("/Location/loc-1", "loc-1"),
        ],
    )
    def test_a_readable_reference_yields_its_id(self, reference: str, expected: str) -> None:
        assert reference_id(Reference(reference=reference), "Location") == expected

    def test_a_urn_uuid_reference_is_not_readable(self) -> None:
        """Only meaningful inside the Bundle that defines it, and we read one at a time."""
        assert reference_id(Reference(reference="urn:uuid:0b7f-1111"), "Location") is None

    def test_a_conditional_reference_is_not_readable(self) -> None:
        """Synthea writes these; HAPI rewrites them at transaction time.

        One surviving into a stored resource means the server did not resolve
        it, so there is no id to read and guessing would fetch the wrong place.
        """
        conditional = "Location?identifier=https://github.com/synthetichealth/synthea|be13c576"

        assert reference_id(Reference(reference=conditional), "Location") is None

    def test_a_reference_to_another_resource_type_is_refused(self) -> None:
        assert reference_id(Reference(reference="Practitioner/p-1"), "Location") is None

    def test_an_absent_reference_is_none(self) -> None:
        assert reference_id(None, "Location") is None


class TestWhichLocationsAreTried:
    def test_a_location_the_patient_never_reached_is_skipped(self) -> None:
        """``planned`` is somewhere they were expected; ``reserved`` is a held room."""
        encounter = an_encounter(
            locations=[
                {"location": {"reference": "Location/planned"}, "status": "planned"},
                {"location": {"reference": "Location/reserved"}, "status": "reserved"},
                {"location": {"reference": "Location/actual"}, "status": "completed"},
            ]
        )

        assert site_location_references(encounter) == ["actual"]

    def test_a_location_with_no_status_is_eligible(self) -> None:
        """The common case, not an edge one — Synthea and both sandboxes omit it.

        Requiring a status would leave every real encounter with no location.
        """
        encounter = an_encounter(locations=[{"location": {"reference": "Location/loc-1"}}])

        assert site_location_references(encounter) == ["loc-1"]

    def test_the_encounters_own_order_is_kept(self) -> None:
        """A visit spans places; the first is tried first and the rest are the fallback."""
        encounter = an_encounter(
            locations=[
                {"location": {"reference": "Location/first"}},
                {"location": {"reference": "Location/second"}},
            ]
        )

        assert site_location_references(encounter) == ["first", "second"]

    def test_the_service_provider_is_read_as_an_organization(self) -> None:
        assert service_provider_reference(an_encounter()) == "org-1"

    def test_an_encounter_with_neither_source_yields_nothing(self) -> None:
        encounter = an_encounter(locations=[], service_provider=None)

        assert site_location_references(encounter) == []
        assert service_provider_reference(encounter) is None


class TestReadingAStateOffAResource:
    def test_a_locations_address_state_is_the_site_of_care(self) -> None:
        location = Location.model_validate(location_resource(state="NH"))

        assert location_state(location) == "NH"

    def test_a_kind_location_is_refused(self) -> None:
        """It describes a class of place, so its address belongs to a template."""
        location = Location.model_validate(location_resource(state="NH", mode="kind"))

        assert location_state(location) is None

    def test_a_location_with_no_address_yields_nothing(self) -> None:
        location = Location.model_validate(location_resource(state=None))

        assert location_state(location) is None

    def test_an_organizations_billing_address_is_not_a_site_of_care(self) -> None:
        """A lockbox sits in another state from every clinic it bills for."""
        organization = Organization.model_validate(
            organization_resource(
                addresses=[
                    {"use": "billing", "state": "DE"},
                    {"state": "MA"},
                ]
            )
        )

        assert organization_state(organization) == "MA"

    def test_an_organization_with_only_a_billing_address_yields_nothing(self) -> None:
        organization = Organization.model_validate(
            organization_resource(addresses=[{"use": "billing", "state": "DE"}])
        )

        assert organization_state(organization) is None

    def test_a_home_address_wins_for_the_patients_residence(self) -> None:
        """A patient can carry a work or temporary address that is not where they live."""
        addresses = [
            Address(use="work", state="NY"),
            Address(use="home", state="MA"),
        ]

        assert patient_address_state(addresses) == "MA"


class TestNormalisation:
    def test_a_cms_sub_state_jurisdiction_code_collapses_to_its_parent(self) -> None:
        """``insurance_policies.state`` is normalised the same way on ingestion.

        A raw ``QN`` in the encounter column would match nothing and read as "no
        policy found", which is the failure this vocabulary exists to prevent.
        """
        assert to_usps_state("QN") == "NY"
        assert to_usps_state("CNMI") == "MP"

    def test_a_lowercase_code_is_accepted(self) -> None:
        assert to_usps_state("ma") == "MA"

    def test_an_unrecognised_state_leaves_the_column_null(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A server spelling it "Massachusetts" must not fail the SMART launch.

        ``payer_vocab.normalize_state`` raises, which is right on the ingestion
        side. Here NULL is already handled downstream and a raw value is not.
        """
        with caplog.at_level(logging.WARNING):
            assert to_usps_state("Massachusetts") is None

        assert "not a USPS or CMS state code" in caplog.text

    def test_nothing_to_normalise_is_none_rather_than_an_error(self) -> None:
        assert to_usps_state(None) is None


class TestTheDisagreementWarning:
    def test_a_disagreement_is_logged_with_both_states(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            log_state_disagreement("NH", "MA")

        assert "NH" in caplog.text
        assert "MA" in caplog.text

    def test_no_identifier_is_logged_alongside_the_states(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A state alone is not a Safe Harbor geographic identifier; a linked one is.

        Those begin *below* the state level, so two bare state codes with nothing
        naming the patient, the encounter or the launch link a state to nobody.
        """
        with caplog.at_level(logging.WARNING):
            log_state_disagreement("NH", "MA")

        for identifier in ("synthea-123", "encounter-1", "Patient/", "launch"):
            assert identifier not in caplog.text

    def test_agreement_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            log_state_disagreement("MA", "MA")

        assert caplog.text == ""

    def test_a_missing_side_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        """Nothing disagreed — one of the two values simply was not there."""
        with caplog.at_level(logging.WARNING):
            log_state_disagreement("MA", None)
            log_state_disagreement(None, "MA")

        assert caplog.text == ""

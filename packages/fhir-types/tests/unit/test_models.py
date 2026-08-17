"""Behaviour of the FHIR R4 models.

These are declarative shapes, so the tests target the places where a shape can
still be wrong at runtime: alias translation in both directions, the reserved-word
elements, required-vs-optional, closed value sets, and the promise that unmodelled
elements survive a round trip.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

import fhir_types as f


class TestAliases:
    """FHIR is camelCase on the wire; the models are snake_case in Python."""

    def test_parses_camel_case_from_a_server(self) -> None:
        patient = f.Patient.model_validate(
            {"resourceType": "Patient", "birthDate": "1975-03-14", "managingOrganization": {}}
        )

        assert patient.birth_date == "1975-03-14"
        assert patient.managing_organization is not None

    def test_constructs_by_python_field_name(self) -> None:
        patient = f.Patient(birth_date="1975-03-14")

        assert patient.model_dump(by_alias=True, exclude_none=True)["birthDate"] == "1975-03-14"

    def test_dumping_without_by_alias_produces_names_a_server_rejects(self) -> None:
        """Guards the documented trap: ``by_alias=True`` is not optional on output."""
        patient = f.Patient(birth_date="1975-03-14")

        assert "birth_date" in patient.model_dump()
        assert "birthDate" not in patient.model_dump()

    def test_exclude_none_drops_unset_elements(self) -> None:
        """FHIR has no null elements — sending one is a validation error on most servers."""
        dumped = f.Patient(id="p1").model_dump(by_alias=True, exclude_none=True)

        assert dumped == {"id": "p1", "resourceType": "Patient"}


class TestReservedWordElements:
    """``class`` is a FHIR element name and a Python keyword on both Encounter and Coverage."""

    def test_encounter_class_round_trips_as_class(self) -> None:
        encounter = f.Encounter.model_validate(
            {"resourceType": "Encounter", "status": "in-progress", "class": {"code": "AMB"}}
        )

        assert encounter.encounter_class is not None
        assert encounter.encounter_class.code == "AMB"
        assert encounter.model_dump(by_alias=True, exclude_none=True)["class"] == {"code": "AMB"}

    def test_encounter_class_can_be_set_by_python_name(self) -> None:
        encounter = f.Encounter(status="finished", encounter_class=f.Coding(code="IMP"))

        assert encounter.model_dump(by_alias=True, exclude_none=True)["class"]["code"] == "IMP"

    def test_coverage_class_round_trips_as_class(self) -> None:
        coverage = f.Coverage.model_validate(
            {
                "resourceType": "Coverage",
                "status": "active",
                "beneficiary": {"reference": "Patient/p1"},
                "payor": [{"display": "Aetna"}],
                "class": [{"type": {"text": "plan"}, "value": "PPO-500"}],
            }
        )

        assert coverage.coverage_class is not None
        assert coverage.coverage_class[0].value == "PPO-500"
        assert "class" in coverage.model_dump(by_alias=True, exclude_none=True)


class TestRequiredElements:
    """FHIR cardinality 1..1 and 1..* elements are required on the model too."""

    def test_condition_requires_a_subject(self) -> None:
        with pytest.raises(ValidationError, match="subject"):
            f.Condition.model_validate({"resourceType": "Condition"})

    def test_coverage_requires_a_payor(self) -> None:
        with pytest.raises(ValidationError, match="payor"):
            f.Coverage.model_validate(
                {
                    "resourceType": "Coverage",
                    "status": "active",
                    "beneficiary": {"reference": "Patient/p1"},
                }
            )

    def test_document_reference_requires_content(self) -> None:
        with pytest.raises(ValidationError, match="content"):
            f.DocumentReference.model_validate(
                {"resourceType": "DocumentReference", "status": "current"}
            )

    def test_patient_requires_nothing_but_its_type(self) -> None:
        """Patient has no 1..1 elements in R4 — an empty one is legal."""
        assert f.Patient().resource_type == "Patient"


class TestClosedValueSets:
    """Required bindings are ``Literal``, so a bad code fails at parse rather than at the payer."""

    def test_rejects_an_unknown_encounter_status(self) -> None:
        with pytest.raises(ValidationError, match="status"):
            f.Encounter.model_validate({"resourceType": "Encounter", "status": "in progress"})

    def test_rejects_an_unknown_claim_use(self) -> None:
        with pytest.raises(ValidationError, match="use"):
            f.Claim.model_validate(
                {
                    "resourceType": "Claim",
                    "status": "active",
                    "type": {"text": "professional"},
                    "use": "prior-auth",
                    "patient": {"reference": "Patient/p1"},
                    "created": "2026-08-17T10:00:00Z",
                    "provider": {"reference": "Organization/o1"},
                    "priority": {"text": "normal"},
                    "insurance": [{"sequence": 1, "focal": True, "coverage": {}}],
                }
            )

    def test_extensible_bindings_stay_open(self) -> None:
        """Condition.code is a CodeableConcept, so any coding system is accepted."""
        condition = f.Condition(
            subject=f.Reference(reference="Patient/p1"),
            code=f.CodeableConcept(
                coding=[f.Coding(system="http://hl7.org/fhir/sid/icd-10-cm", code="M17.11")]
            ),
        )

        assert condition.code is not None
        assert condition.code.coding is not None
        assert condition.code.coding[0].code == "M17.11"


class TestPartialDates:
    """FHIR dates permit reduced precision, which is why they are modelled as ``str``."""

    @pytest.mark.parametrize("birth_date", ["1975", "1975-03", "1975-03-14"])
    def test_accepts_every_legal_date_precision(self, birth_date: str) -> None:
        assert f.Patient(birth_date=birth_date).birth_date == birth_date


class TestUnmodelledElements:
    """``extra="allow"`` — a server sends far more than this package models."""

    def test_unknown_elements_survive_a_round_trip(self) -> None:
        payload = {
            "resourceType": "Patient",
            "id": "p1",
            "extension": [{"url": "http://example.org/race", "valueString": "unknown"}],
            "multipleBirthInteger": 2,
        }

        dumped = f.Patient.model_validate(payload).model_dump(by_alias=True, exclude_none=True)

        assert dumped == payload

    def test_an_unmodelled_element_is_reachable(self) -> None:
        patient = f.Patient.model_validate({"resourceType": "Patient", "photo": []})

        assert patient.model_extra == {"photo": []}


class TestImmutability:
    """The models are frozen — a resource read from an EHR is a snapshot."""

    def test_assignment_is_rejected(self) -> None:
        patient = f.Patient(id="p1")

        with pytest.raises(ValidationError, match="frozen"):
            patient.id = "p2"  # type: ignore[misc]

    def test_model_copy_is_the_supported_way_to_change_one(self) -> None:
        patient = f.Patient(id="p1")

        assert patient.model_copy(update={"id": "p2"}).id == "p2"
        assert patient.id == "p1"


class TestAnyResource:
    """The discriminated union parses a resource whose type is not known ahead of time."""

    ADAPTER = TypeAdapter(f.AnyResource)

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"resourceType": "Patient"}, f.Patient),
            (
                {
                    "resourceType": "Condition",
                    "subject": {"reference": "Patient/p1"},
                },
                f.Condition,
            ),
            (
                {
                    "resourceType": "MedicationRequest",
                    "status": "active",
                    "intent": "order",
                    "subject": {"reference": "Patient/p1"},
                },
                f.MedicationRequest,
            ),
        ],
    )
    def test_narrows_on_resource_type(self, payload: dict[str, object], expected: type) -> None:
        assert isinstance(self.ADAPTER.validate_python(payload), expected)

    def test_an_unmodelled_resource_type_raises(self) -> None:
        """Better to fail loudly than to validate an Observation as something else."""
        with pytest.raises(ValidationError):
            self.ADAPTER.validate_python({"resourceType": "Observation", "status": "final"})


class TestPriorAuthorizationBundle:
    """The shape TASK-004 exists to support, end to end.

    A prior authorization is a Claim with ``use = "preauthorization"`` whose items
    point at the diagnoses and supporting evidence that justify them. This asserts
    the cross-references survive a validate/dump round trip intact, since a
    mis-sequenced pointer is the kind of error a payer rejects rather than explains.
    """

    def build(self) -> f.Claim:
        return f.Claim(
            status="active",
            type=f.CodeableConcept(text="professional"),
            use="preauthorization",
            patient=f.Reference(reference="Patient/p1"),
            created="2026-08-17T10:00:00Z",
            insurer=f.Reference(display="Aetna"),
            provider=f.Reference(reference="Organization/ortho-1"),
            priority=f.CodeableConcept(text="normal"),
            diagnosis=[
                f.ClaimDiagnosis(
                    sequence=1,
                    diagnosis_codeable_concept=f.CodeableConcept(
                        coding=[
                            f.Coding(code="M17.11", display="Unilateral primary osteoarthritis")
                        ]
                    ),
                )
            ],
            supporting_info=[
                f.ClaimSupportingInfo(
                    sequence=1,
                    category=f.CodeableConcept(text="conservative-treatment"),
                    value_string="12 weeks physical therapy, no improvement",
                )
            ],
            insurance=[
                f.ClaimInsurance(
                    sequence=1, focal=True, coverage=f.Reference(reference="Coverage/c1")
                )
            ],
            item=[
                f.ClaimItem(
                    sequence=1,
                    diagnosis_sequence=[1],
                    information_sequence=[1],
                    product_or_service=f.CodeableConcept(
                        coding=[f.Coding(code="27447", display="Total knee arthroplasty")]
                    ),
                )
            ],
        )

    def test_round_trips_unchanged(self) -> None:
        claim = self.build()
        dumped = claim.model_dump(by_alias=True, exclude_none=True)

        assert f.Claim.model_validate(dumped) == claim

    def test_item_pointers_serialize_under_their_fhir_names(self) -> None:
        item = self.build().model_dump(by_alias=True, exclude_none=True)["item"][0]

        assert item["diagnosisSequence"] == [1]
        assert item["informationSequence"] == [1]
        assert item["productOrService"]["coding"][0]["code"] == "27447"

    def test_use_marks_it_as_an_authorization_not_a_bill(self) -> None:
        assert self.build().use == "preauthorization"


def test_fhir_version_is_r4() -> None:
    """R4, not R4B or R5 — see CLAUDE.md."""
    assert f.FHIR_VERSION == "4.0.1"

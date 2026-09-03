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


class TestUnknownResource:
    """A bundle entry of a type this package does not model.

    A PAS response carries referenced resources, and "referenced" is open —
    ``Practitioner``, ``PractitionerRole``, ``Task`` and ``OperationOutcome`` are
    all conformant there. Parsing one must not raise and must not lose anything.
    """

    ADAPTER = TypeAdapter(f.AnyResourceOrUnknown)

    PRACTITIONER = {
        "resourceType": "Practitioner",
        "id": "pr-1",
        "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567893"}],
        "name": [{"family": "Okafor", "given": ["Ada"], "suffix": ["MD"]}],
    }

    def test_an_unmodelled_type_parses_as_unknown(self) -> None:
        parsed = self.ADAPTER.validate_python(self.PRACTITIONER)

        assert isinstance(parsed, f.UnknownResource)

    def test_nothing_is_lost(self) -> None:
        """The whole point: never coerced into a modelled shape, never discarded."""
        parsed = self.ADAPTER.validate_python(self.PRACTITIONER)

        assert parsed.model_dump(by_alias=True, exclude_none=True) == self.PRACTITIONER

    def test_the_resource_type_is_readable(self) -> None:
        parsed = self.ADAPTER.validate_python(self.PRACTITIONER)

        assert parsed.resource_type == "Practitioner"

    def test_a_modelled_type_still_narrows(self) -> None:
        parsed = self.ADAPTER.validate_python({"resourceType": "Patient", "id": "p1"})

        assert isinstance(parsed, f.Patient)

    def test_a_malformed_modelled_type_raises_rather_than_falling_back(self) -> None:
        """The branch is chosen by ``resourceType``, not by whatever validates.

        A Claim missing its required ``status`` is an error. Accepting it as an
        ``UnknownResource`` would silently downgrade a payload this package does
        understand into one it does not.
        """
        with pytest.raises(ValidationError):
            self.ADAPTER.validate_python({"resourceType": "Claim"})

    def test_a_payload_with_no_resource_type_raises(self) -> None:
        with pytest.raises(ValidationError):
            self.ADAPTER.validate_python({"id": "nothing-says-what-this-is"})

    def test_resource_type_is_none_when_constructed_without_one(self) -> None:
        assert f.UnknownResource().resource_type is None

    def test_one_can_be_built_for_an_outbound_bundle(self) -> None:
        """TASK-054 has to *send* a Practitioner, which is unmodelled here too."""
        practitioner = f.UnknownResource(**self.PRACTITIONER)

        assert practitioner.model_dump(by_alias=True, exclude_none=True) == self.PRACTITIONER


class TestPriorAuthorizationRequestBundle:
    """``Claim/$submit`` takes a Bundle: one Claim plus everything it references.

    Da Vinci PAS profiles it as ``profile-pas-request-bundle``. The Claim points at
    its Patient, Coverage and provider entries by ``fullUrl``, so an entry that
    loses that element breaks the request in a way the payer reports as a missing
    resource rather than as a bad link — which is why the round trip is asserted
    over the whole bundle rather than resource by resource.
    """

    PAYLOAD: dict[str, object] = {
        "resourceType": "Bundle",
        "type": "collection",
        "timestamp": "2026-08-17T10:04:00Z",
        "entry": [
            {
                "fullUrl": "urn:uuid:claim-1",
                "resource": {
                    "resourceType": "Claim",
                    "status": "active",
                    "type": {"text": "professional"},
                    "use": "preauthorization",
                    "patient": {"reference": "urn:uuid:patient-1"},
                    "created": "2026-08-17T10:04:00Z",
                    "insurer": {"display": "Aetna"},
                    "provider": {"reference": "urn:uuid:practitioner-1"},
                    "priority": {"text": "normal"},
                    "diagnosis": [
                        {
                            "sequence": 1,
                            "diagnosisCodeableConcept": {
                                "coding": [{"code": "M17.11", "display": "OA, right knee"}]
                            },
                        }
                    ],
                    "supportingInfo": [
                        {
                            "sequence": 1,
                            "category": {"text": "conservative-treatment"},
                            "valueString": "12 weeks physical therapy, no improvement",
                        }
                    ],
                    "insurance": [
                        {
                            "sequence": 1,
                            "focal": True,
                            "coverage": {"reference": "urn:uuid:coverage-1"},
                        }
                    ],
                    "item": [
                        {
                            "sequence": 1,
                            "diagnosisSequence": [1],
                            "informationSequence": [1],
                            "productOrService": {"coding": [{"code": "27447"}]},
                        }
                    ],
                },
            },
            {
                "fullUrl": "urn:uuid:patient-1",
                "resource": {"resourceType": "Patient", "id": "patient-1", "gender": "female"},
            },
            {
                "fullUrl": "urn:uuid:coverage-1",
                "resource": {
                    "resourceType": "Coverage",
                    "status": "active",
                    "beneficiary": {"reference": "urn:uuid:patient-1"},
                    "payor": [{"display": "Aetna"}],
                },
            },
            {
                "fullUrl": "urn:uuid:practitioner-1",
                "resource": {"resourceType": "Practitioner", "id": "practitioner-1"},
            },
        ],
    }

    def parsed(self) -> f.Bundle:
        return f.Bundle.model_validate(self.PAYLOAD)

    def test_round_trips_unchanged(self) -> None:
        dumped = self.parsed().model_dump(by_alias=True, exclude_none=True)

        assert dumped == self.PAYLOAD

    def test_the_claim_entry_narrows_to_a_claim(self) -> None:
        claim = self.parsed().entry[0].resource

        assert isinstance(claim, f.Claim)
        assert claim.use == "preauthorization"

    def test_the_referenced_practitioner_survives_unmodelled(self) -> None:
        practitioner = self.parsed().entry[3].resource

        assert isinstance(practitioner, f.UnknownResource)
        assert practitioner.resource_type == "Practitioner"

    def test_full_urls_are_kept_so_intra_bundle_references_resolve(self) -> None:
        bundle = self.parsed()
        claim = bundle.entry[0].resource
        assert isinstance(claim, f.Claim)

        assert claim.patient.reference == bundle.entry[1].full_url


class TestPriorAuthorizationResponseBundle:
    """``Claim/$submit`` answers with a Bundle carrying a ClaimResponse.

    The fields that matter downstream are ``preAuthRef`` — the number a later claim
    quotes — and ``item.adjudication``, which is where an authorization is actually
    approved or denied. ``outcome`` says only that the payer processed the request.
    """

    PAYLOAD: dict[str, object] = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "fullUrl": "urn:uuid:claimresponse-1",
                "resource": {
                    "resourceType": "ClaimResponse",
                    "status": "active",
                    "type": {"text": "professional"},
                    "use": "preauthorization",
                    "patient": {"reference": "urn:uuid:patient-1"},
                    "created": "2026-08-17T10:04:09Z",
                    "insurer": {"display": "Aetna"},
                    "request": {"reference": "urn:uuid:claim-1"},
                    "outcome": "complete",
                    "disposition": "Prior authorization approved",
                    "preAuthRef": "AUTH-88213",
                    "preAuthPeriod": {"start": "2026-08-17", "end": "2026-11-15"},
                    "item": [
                        {
                            "itemSequence": 1,
                            "noteNumber": [1],
                            "adjudication": [
                                {
                                    "category": {"coding": [{"code": "submitted"}]},
                                    "reason": {"coding": [{"code": "approved"}]},
                                }
                            ],
                        }
                    ],
                    "insurance": [
                        {
                            "sequence": 1,
                            "focal": True,
                            "coverage": {"reference": "urn:uuid:coverage-1"},
                        }
                    ],
                    "processNote": [
                        {
                            "number": 1,
                            "type": "display",
                            "text": "Approved for one procedure within 90 days.",
                        }
                    ],
                },
            },
            {
                "fullUrl": "urn:uuid:task-1",
                "resource": {
                    "resourceType": "Task",
                    "status": "completed",
                    "intent": "order",
                    "focus": {"reference": "urn:uuid:claim-1"},
                },
            },
        ],
    }

    def parsed(self) -> f.Bundle:
        return f.Bundle.model_validate(self.PAYLOAD)

    def test_round_trips_unchanged(self) -> None:
        dumped = self.parsed().model_dump(by_alias=True, exclude_none=True)

        assert dumped == self.PAYLOAD

    def response(self) -> f.ClaimResponse:
        response = self.parsed().entry[0].resource
        assert isinstance(response, f.ClaimResponse)
        return response

    def test_the_authorization_number_is_reachable(self) -> None:
        assert self.response().pre_auth_ref == "AUTH-88213"

    def test_the_authorization_carries_the_period_it_is_valid_for(self) -> None:
        """An approval read without this looks open-ended when it is not."""
        assert self.response().pre_auth_period.end == "2026-11-15"

    def test_the_decision_is_in_the_adjudication_not_the_outcome(self) -> None:
        response = self.response()

        assert response.outcome == "complete"
        assert response.item[0].adjudication[0].reason.coding[0].code == "approved"

    def test_a_referenced_task_survives_unmodelled(self) -> None:
        task = self.parsed().entry[1].resource

        assert isinstance(task, f.UnknownResource)
        assert task.resource_type == "Task"

    def test_a_pended_response_reports_its_errors(self) -> None:
        pended = f.ClaimResponse.model_validate(
            {
                "resourceType": "ClaimResponse",
                "status": "active",
                "type": {"text": "professional"},
                "use": "preauthorization",
                "patient": {"reference": "Patient/p1"},
                "created": "2026-08-17T10:04:09Z",
                "insurer": {"display": "Aetna"},
                "outcome": "error",
                "error": [{"itemSequence": 1, "code": {"coding": [{"code": "missing-info"}]}}],
            }
        )

        assert pended.pre_auth_ref is None
        assert pended.error[0].code.coding[0].code == "missing-info"


class TestBundleEntryMetadata:
    """``search``/``request``/``response`` are modelled though PAS prohibits them.

    This package models R4; a profile's constraints are the caller's business.
    TASK-054's builder is what satisfies the profile.
    """

    def test_a_transaction_entry_round_trips(self) -> None:
        payload = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "resource": {"resourceType": "Patient", "id": "p1"},
                    "request": {"method": "POST", "url": "Patient", "ifNoneExist": "identifier=1"},
                }
            ],
        }

        dumped = f.Bundle.model_validate(payload).model_dump(by_alias=True, exclude_none=True)

        assert dumped == payload

    def test_a_search_result_entry_round_trips(self) -> None:
        payload = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": 1,
            "link": [{"relation": "self", "url": "https://ehr.example.org/Patient?_id=p1"}],
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "p1"}, "search": {"mode": "match"}}
            ],
        }

        dumped = f.Bundle.model_validate(payload).model_dump(by_alias=True, exclude_none=True)

        assert dumped == payload

    def test_an_operation_outcome_in_a_response_stays_intact(self) -> None:
        """``OperationOutcome`` is unmodelled, so the entry keeps it as unknown."""
        entry = f.BundleEntry.model_validate(
            {
                "response": {
                    "status": "400 Bad Request",
                    "outcome": {
                        "resourceType": "OperationOutcome",
                        "issue": [{"severity": "error", "code": "required"}],
                    },
                }
            }
        )

        assert isinstance(entry.response.outcome, f.UnknownResource)
        assert entry.response.outcome.model_extra["issue"][0]["code"] == "required"

    def test_a_bundle_can_carry_a_bundle(self) -> None:
        """``Bundle`` is in ``AnyResource``, so entries nest without special-casing."""
        nested = f.Bundle.model_validate(
            {
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [{"resource": {"resourceType": "Bundle", "type": "collection"}}],
            }
        )

        assert isinstance(nested.entry[0].resource, f.Bundle)


def test_fhir_version_is_r4() -> None:
    """R4, not R4B or R5 — see CLAUDE.md."""
    assert f.FHIR_VERSION == "4.0.1"

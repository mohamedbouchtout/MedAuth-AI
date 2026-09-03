"""The PAS bundle builder and response reader (TASK-054).

The route tests assert the profile's rules on the bytes that actually went on
the wire, which is where they belong. What is worth testing directly here is the
reading half — what this does with a payer's answer — and the refusals, which
have to happen before anything leaves the system rather than as a rejection from
the payer.
"""

from __future__ import annotations

import pytest

from fhir_types import Bundle
from src.adapters.models import (
    CoverageInfo,
    NoteCode,
    PriorAuthContent,
    PriorAuthEvidence,
    PriorAuthProcedure,
    SubmissionOutcome,
)
from src.adapters.pas_bundle import (
    PriorAuthNotSubmittable,
    build_request_bundle,
    payer_reference_number,
    read_response_bundle,
    submission_outcome,
    unknown_entry_types,
)

PRACTITIONER = "https://ehr.example.org/fhir/Practitioner/pr-1"


def content(**overrides: object) -> PriorAuthContent:
    fields: dict[str, object] = {
        "request_id": "request-1",
        "patient_id": "patient-7",
        "encounter_id": "encounter-4",
        "provider_reference": PRACTITIONER,
        "payer_name": "Aetna",
        "coverage": CoverageInfo(payer="Aetna", plan_type="PPO", member_id="W1"),
        "procedures": [PriorAuthProcedure(cpt_code="27447", description="knee replacement")],
        "icd10_codes": [NoteCode(code="M17.11", source="llm-extraction")],
        "clinical_evidence": [PriorAuthEvidence(text="12 weeks of physical therapy")],
    }
    fields.update(overrides)
    return PriorAuthContent(**fields)  # type: ignore[arg-type]


def response_bundle(
    *, outcome: str = "complete", pre_auth_ref: str | None = "AUTH-1", extra: bool = True
) -> Bundle:
    claim_response: dict[str, object] = {
        "resourceType": "ClaimResponse",
        "status": "active",
        "type": {"text": "professional"},
        "use": "preauthorization",
        "patient": {"reference": "Patient/patient-7"},
        "created": "2026-09-03T10:04:09Z",
        "insurer": {"display": "Aetna"},
        "outcome": outcome,
    }
    if pre_auth_ref is not None:
        claim_response["preAuthRef"] = pre_auth_ref
    entries: list[dict[str, object]] = [{"resource": claim_response}]
    if extra:
        entries.append({"resource": {"resourceType": "Task", "status": "completed"}})
    return Bundle.model_validate({"resourceType": "Bundle", "type": "collection", "entry": entries})


def test_the_claim_is_the_first_entry() -> None:
    """ClaimFirst — the profile's invariant, and the order of the list is its whole
    implementation."""
    bundle = build_request_bundle(content())

    assert bundle.entry is not None
    assert bundle.entry[0].resource is not None
    assert bundle.entry[0].resource.resource_type == "Claim"


def test_the_bundle_identifies_itself_uniquely_per_submission() -> None:
    """Reusing one would let a payer treat two requests as one."""
    first = build_request_bundle(content())
    second = build_request_bundle(content())

    assert first.identifier is not None and second.identifier is not None
    assert first.identifier.value != second.identifier.value


def test_the_evidence_becomes_supporting_info_the_items_point_at() -> None:
    """A payer's criteria are evaluated against these entries."""
    bundle = build_request_bundle(content())
    claim = bundle.entry[0].resource  # type: ignore[index, union-attr]

    assert claim.supporting_info[0].value_string == "12 weeks of physical therapy"  # type: ignore[union-attr, index]
    assert claim.item[0].information_sequence == [1]  # type: ignore[union-attr, index]


def test_a_request_with_no_procedure_is_refused_before_anything_is_sent() -> None:
    with pytest.raises(PriorAuthNotSubmittable, match="no procedure"):
        build_request_bundle(content(procedures=[]))


def test_a_request_with_no_verified_provider_is_refused() -> None:
    """``Claim.provider`` is 1..1, and the alternative is asserting who is asking."""
    with pytest.raises(PriorAuthNotSubmittable, match="no verified provider"):
        build_request_bundle(content(provider_reference=None))


def test_a_request_with_no_coverage_still_carries_a_coverage_resource() -> None:
    """``Claim.insurance`` is required; what is unknown is left absent, not invented."""
    bundle = build_request_bundle(content(coverage=None))
    coverage = bundle.entry[1].resource  # type: ignore[index, union-attr]

    assert coverage.resource_type == "Coverage"  # type: ignore[union-attr]
    assert coverage.subscriber_id is None  # type: ignore[union-attr]


def test_the_claim_response_is_read_out_of_the_bundle() -> None:
    """The operation returns a bundle, never a bare ClaimResponse."""
    assert read_response_bundle(response_bundle()).pre_auth_ref == "AUTH-1"


def test_an_unmodelled_entry_does_not_stop_the_answer_being_read() -> None:
    """A PAS response conformantly carries Practitioner, Task and OperationOutcome."""
    bundle = response_bundle(extra=True)

    assert unknown_entry_types(bundle) == ["Task"]
    assert read_response_bundle(bundle).outcome == "complete"


def test_a_response_with_no_claim_response_raises() -> None:
    """An OperationOutcome-only answer is no determination, not a silent success."""
    bundle = Bundle.model_validate(
        {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": {"resourceType": "OperationOutcome", "issue": []}}],
        }
    )

    with pytest.raises(PriorAuthNotSubmittable, match="no ClaimResponse"):
        read_response_bundle(bundle)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("complete", SubmissionOutcome.COMPLETE),
        ("queued", SubmissionOutcome.QUEUED),
        ("partial", SubmissionOutcome.PARTIAL),
        ("error", SubmissionOutcome.ERROR),
    ],
)
def test_every_conformant_outcome_maps(code: str, expected: SubmissionOutcome) -> None:
    response = read_response_bundle(response_bundle(outcome=code, pre_auth_ref=None))

    assert submission_outcome(response) is expected


def test_a_queued_answer_with_no_reference_is_not_a_failure() -> None:
    """``preAuthRef`` is 0..1 and only present on an adjudicated preauthorization."""
    response = read_response_bundle(response_bundle(outcome="queued", pre_auth_ref=None))

    assert payer_reference_number(response) is None

"""The normalized shapes the adapter layer returns.

These models are typed stubs' return values and nothing populates them yet, so
what is worth asserting is only what a later task could break by accident: the
defaults that distinguish "the EHR said nothing" from "the EHR said none".
"""

from __future__ import annotations

from src.adapters.models import CoverageInfo, PatientContext, PatientInfo


def test_a_patient_context_needs_only_the_patient() -> None:
    """Coverage and conditions are absent far more often than they are wrong."""
    context = PatientContext(patient=PatientInfo(patient_id="patient-1"))

    assert context.coverage is None
    assert context.conditions == []
    assert context.requires_manual_confirmation is False


def test_manual_confirmation_is_opt_in_rather_than_inferred() -> None:
    """TASK-052 sets this deliberately when payer info is incomplete.

    A default of True would make every context look like it needed a human, and
    a value inferred from an empty coverage would guess at what the EHR meant.
    """
    context = PatientContext(
        patient=PatientInfo(patient_id="patient-1"),
        coverage=CoverageInfo(payer="Aetna"),
        requires_manual_confirmation=True,
    )

    assert context.requires_manual_confirmation is True
    assert context.coverage is not None
    assert context.coverage.plan_type is None


def test_the_payer_is_kept_as_the_resource_spelled_it() -> None:
    """Slugging happens once, in /policies/query. Not here, and not twice."""
    coverage = CoverageInfo(payer="Aetna Better Health of MA", plan_type="PPO", member_id="W123")

    assert coverage.payer == "Aetna Better Health of MA"

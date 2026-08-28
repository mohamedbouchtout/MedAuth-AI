"""Stage 2: what this encounter has not documented, and how risky that is.

Two things are being asserted here. The first is the behaviour of the matching
heuristic — deterministic, and biased toward reporting a criterion as missing,
because a criterion wrongly flagged costs a glance and a criterion wrongly
cleared costs a denied authorization.

The second is that nothing patient-specific escapes into the text this module
produces. ``missing_criteria`` echoes the payer's own words and
``nudge_message`` is built from those words and the procedure name; the clinical
context is read and never repeated. That matters because the nudge is relayed
over a WebSocket to a browser.
"""

from __future__ import annotations

import pytest

from track_b_rag import gap_analysis
from track_b_rag.policy_rules import PolicyRules

CRITERIA = [
    "Failed six weeks of conservative therapy",
    "Documented neurological deficit on examination",
]


def rules(**overrides: object) -> PolicyRules:
    """Build a Stage 1 answer, overriding whichever fields a test cares about."""
    values: dict[str, object] = {
        "requires_auth": True,
        "auth_criteria": list(CRITERIA),
        "step_therapy_required": False,
        "step_therapy_details": None,
    }
    values.update(overrides)
    return PolicyRules.model_validate(values)


# --- matching --------------------------------------------------------------


def test_a_criterion_the_note_covers_is_not_missing() -> None:
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=["Failed six weeks of conservative therapy"]),
        clinical_context={"hpi": "Failed conservative therapy over six weeks of physical therapy"},
        procedure="knee MRI",
    )

    assert assessment.missing_criteria == []


def test_a_criterion_the_note_says_nothing_about_is_missing() -> None:
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=["Documented neurological deficit on examination"]),
        clinical_context={"hpi": "Patient reports knee pain after a fall"},
        procedure="knee MRI",
    )

    assert assessment.missing_criteria == ["Documented neurological deficit on examination"]


def test_structured_context_keys_count_as_documentation() -> None:
    """``{"conservative_therapy_failed": true}`` says as much as a sentence does."""
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=["Failed conservative therapy"]),
        clinical_context={"conservative_therapy_failed": True},
        procedure="knee MRI",
    )

    assert assessment.missing_criteria == []


def test_nested_context_is_read_all_the_way_down() -> None:
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=["Documented neurological deficit"]),
        clinical_context={"exam": {"findings": ["neurological deficit noted"]}},
        procedure="knee MRI",
    )

    assert assessment.missing_criteria == []


def test_an_empty_context_leaves_every_criterion_missing() -> None:
    assessment = gap_analysis.assess(rules=rules(), clinical_context={}, procedure="knee MRI")

    assert assessment.missing_criteria == CRITERIA


def test_a_criterion_written_entirely_in_boilerplate_is_reported_missing() -> None:
    """There is nothing to check it against, so it is one for a human to read."""
    assert gap_analysis.is_documented("The patient must have documentation", frozenset()) is False


def test_matching_ignores_case_and_punctuation() -> None:
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=["Failed conservative therapy."]),
        clinical_context={"note": "FAILED CONSERVATIVE THERAPY!"},
        procedure="knee MRI",
    )

    assert assessment.missing_criteria == []


def test_a_null_value_in_the_context_contributes_nothing() -> None:
    """A structured context often carries explicit nulls for fields not yet charted.

    "Not documented" must not become the word "None" in the vocabulary, where it
    could match a criterion by accident.
    """
    terms = gap_analysis.context_vocabulary({"imaging": None, "exam": "deficit noted"})

    assert "none" not in terms
    assert "deficit" in terms


def test_numbers_are_part_of_a_criterion() -> None:
    """ "Six weeks" and "twelve weeks" of therapy are different requirements."""
    terms = gap_analysis.context_vocabulary({"note": "12 weeks of therapy"})

    assert "12" in terms


# --- denial risk -----------------------------------------------------------


def test_no_authorization_required_is_low_risk() -> None:
    assessment = gap_analysis.assess(
        rules=rules(requires_auth=False, auth_criteria=[]),
        clinical_context={},
        procedure="knee MRI",
    )

    assert assessment.denial_risk == "low"


def test_everything_documented_is_low_risk() -> None:
    assessment = gap_analysis.assess(
        rules=rules(),
        clinical_context={
            "note": "Failed six weeks of conservative therapy; neurological deficit on examination"
        },
        procedure="knee MRI",
    )

    assert assessment.missing_criteria == []
    assert assessment.denial_risk == "low"


def test_half_the_criteria_missing_is_medium_risk() -> None:
    assessment = gap_analysis.assess(
        rules=rules(),
        clinical_context={"note": "Failed six weeks of conservative therapy"},
        procedure="knee MRI",
    )

    assert len(assessment.missing_criteria) == 1
    assert assessment.denial_risk == "medium"


def test_most_of_the_criteria_missing_is_high_risk() -> None:
    assessment = gap_analysis.assess(rules=rules(), clinical_context={}, procedure="knee MRI")

    assert assessment.denial_risk == "high"


def test_authorization_required_with_no_known_criteria_is_medium_not_low() -> None:
    """An empty criteria list here means "not known", not "none"."""
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=[]), clinical_context={}, procedure="knee MRI"
    )

    assert assessment.denial_risk == "medium"


def test_step_therapy_lifts_the_floor_to_medium() -> None:
    assessment = gap_analysis.assess(
        rules=rules(
            requires_auth=False,
            auth_criteria=[],
            step_therapy_required=True,
            step_therapy_details="NSAIDs must be tried first",
        ),
        clinical_context={},
        procedure="biologic injection",
    )

    assert assessment.denial_risk == "medium"


def test_step_therapy_does_not_escalate_an_already_high_risk_further() -> None:
    """It raises the floor; there is nothing above high to raise it to."""
    assessment = gap_analysis.assess(
        rules=rules(step_therapy_required=True, step_therapy_details="NSAIDs first"),
        clinical_context={},
        procedure="knee MRI",
    )

    assert assessment.denial_risk == "high"


# --- the nudge -------------------------------------------------------------


def test_the_nudge_names_the_procedure_and_what_is_missing() -> None:
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=["Documented neurological deficit on examination"]),
        clinical_context={},
        procedure="knee MRI",
    )

    assert "knee MRI" in assessment.nudge_message
    assert "neurological deficit" in assessment.nudge_message


def test_nothing_required_says_nothing_at_all() -> None:
    """Silence, not "no prior authorization required".

    The message is the nudge trigger (CLAUDE.md, "The nudge trigger is the
    message"), so anything returned here is something a provider is interrupted
    with mid-consultation. Confirming that a procedure needs no authorization is
    not worth an interruption, and this branch returning prose is what made the
    message useless as a signal — every branch was non-empty, so triggering on
    "non-empty" would have nudged on every query in the product.
    """
    assessment = gap_analysis.assess(
        rules=rules(requires_auth=False, auth_criteria=[]),
        clinical_context={},
        procedure="knee MRI",
    )

    assert assessment.nudge_message is None


def test_everything_documented_says_nothing_at_all() -> None:
    """The other silent case: authorization required and nothing is missing."""
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=["Failed conservative therapy"]),
        clinical_context={"note": "failed conservative therapy"},
        procedure="knee MRI",
    )

    assert assessment.nudge_message is None


def test_the_nudge_asks_for_a_manual_check_when_no_criteria_were_found() -> None:
    """Regression: this composed a message that nothing would ever show.

    Authorization is required and no published criteria could be found, which
    ``denial_risk()`` scores ``medium`` — deliberately, because "no criteria"
    here means *not known* rather than *none*. Under TASK-040's original
    trigger, ``missing_criteria`` non-empty or ``denial_risk == "high"``,
    neither leg fires, so the provider was asked to confirm the requirements
    manually by a message no consumer would ever emit. The trigger is now the
    message itself, so this case reaches a provider.
    """
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=[]), clinical_context={}, procedure="knee MRI"
    )

    assert assessment.nudge_message is not None
    assert "confirm the requirements manually" in assessment.nudge_message
    assert not assessment.missing_criteria
    assert assessment.denial_risk == "medium"


def test_step_therapy_alone_still_nudges() -> None:
    """Regression: the second case the original trigger scored below.

    Every criterion is documented and the only outstanding requirement is step
    therapy, which lifts the risk floor to ``medium`` with nothing missing —
    below both legs of the original trigger. It has to reach the provider:
    step therapy is a prerequisite the payer checks before it will consider the
    request at all, so an otherwise perfect submission still fails on it.
    """
    assessment = gap_analysis.assess(
        rules=rules(
            auth_criteria=["Failed conservative therapy"],
            step_therapy_required=True,
            step_therapy_details="NSAIDs for six weeks first",
        ),
        clinical_context={"note": "failed conservative therapy"},
        procedure="knee MRI",
    )

    assert assessment.nudge_message is not None
    assert "step therapy" in assessment.nudge_message
    assert not assessment.missing_criteria
    assert assessment.denial_risk == "medium"


def test_step_therapy_nudges_even_when_no_authorization_is_required() -> None:
    """Step therapy overrides the no-authorization silence, for the same reason.

    A plan can require a first-line therapy without requiring prior
    authorization, and the provider still needs to know before ordering.
    """
    assessment = gap_analysis.assess(
        rules=rules(
            requires_auth=False,
            auth_criteria=[],
            step_therapy_required=True,
            step_therapy_details="NSAIDs for six weeks first",
        ),
        clinical_context={},
        procedure="knee MRI",
    )

    assert assessment.nudge_message is not None
    assert "step therapy" in assessment.nudge_message
    assert assessment.denial_risk == "medium"


def test_the_nudge_mentions_step_therapy_and_its_details() -> None:
    assessment = gap_analysis.assess(
        rules=rules(
            auth_criteria=[],
            step_therapy_required=True,
            step_therapy_details="NSAIDs for six weeks first",
        ),
        clinical_context={},
        procedure="biologic injection",
    )

    assert "step therapy" in assessment.nudge_message
    assert "NSAIDs for six weeks first" in assessment.nudge_message


def test_step_therapy_with_no_details_still_reads_as_a_sentence() -> None:
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=[], step_therapy_required=True),
        clinical_context={},
        procedure="biologic injection",
    )

    assert "first-line therapy must be tried first" in assessment.nudge_message


def test_a_long_criteria_list_is_summarised_rather_than_recited() -> None:
    """A nudge is read mid-encounter by someone talking to a patient."""
    assessment = gap_analysis.assess(
        rules=rules(auth_criteria=[f"Criterion number {index} charted" for index in range(6)]),
        clinical_context={},
        procedure="knee MRI",
    )

    assert "and 3 more" in assessment.nudge_message


def test_the_nudge_never_repeats_the_clinical_context() -> None:
    """It is relayed to a browser over a WebSocket; PHI has no business in it."""
    assessment = gap_analysis.assess(
        rules=rules(),
        clinical_context={
            "patient_name": "Zebulon Quackenbush",
            "mrn": "MRN-8675309",
            "hpi": "Sustained a fall while skiing in Vermont",
        },
        procedure="knee MRI",
    )

    assert "Quackenbush" not in assessment.nudge_message
    assert "8675309" not in assessment.nudge_message
    assert "skiing" not in assessment.nudge_message


def test_missing_criteria_echo_the_payer_not_the_patient() -> None:
    assessment = gap_analysis.assess(
        rules=rules(),
        clinical_context={"mrn": "MRN-8675309"},
        procedure="knee MRI",
    )

    assert assessment.missing_criteria == CRITERIA


# --- the guarantee the whole design rests on -------------------------------


def test_two_patients_on_one_plan_get_different_gaps() -> None:
    """Same payer rules object, different documentation, different answers.

    This is the unit-level statement of the correctness test TASK-012 asks for
    at the HTTP level: Stage 2 is a function of the clinical context, so it
    cannot be shared between patients the way Stage 1 is.
    """
    payer_rules = rules()

    first = gap_analysis.assess(
        rules=payer_rules,
        clinical_context={"note": "Failed six weeks of conservative therapy"},
        procedure="knee MRI",
    )
    second = gap_analysis.assess(
        rules=payer_rules,
        clinical_context={"note": "Neurological deficit on examination"},
        procedure="knee MRI",
    )

    assert first.missing_criteria != second.missing_criteria


@pytest.mark.parametrize("run", range(3))
def test_the_same_inputs_give_the_same_answer_every_time(run: int) -> None:
    """Deterministic by construction — no model call, no sampling, no clock."""
    assessment = gap_analysis.assess(
        rules=rules(),
        clinical_context={"note": "Failed six weeks of conservative therapy"},
        procedure="knee MRI",
    )

    assert assessment.missing_criteria == ["Documented neurological deficit on examination"]
    assert assessment.denial_risk == "medium"

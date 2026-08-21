"""Joining the two stages: what gets combined, and what happens when Stage 1 fails.

The orchestration is thin, and the two things worth asserting about it are both
about the seam. Stage 2 runs on every call, including the ones Stage 1 served
from cache — that is what keeps two patients on one plan from sharing an
answer. And when Stage 1 falls back, Stage 2 does not run at all, because
comparing a note against criteria nobody knows would produce an empty
``missing_criteria`` that reads as "nothing is missing".
"""

from __future__ import annotations

import pytest

from track_b_rag import policy_rules, query
from track_b_rag.policy_rules import PolicyRules, PolicyRulesResolution

CRITERIA = [
    "Failed six weeks of conservative therapy",
    "Documented neurological deficit on examination",
]


class Stage1:
    """Stands in for the Stage 1 seam, recording what it was asked."""

    def __init__(self, resolution: PolicyRulesResolution) -> None:
        self.resolution = resolution
        self.calls: list[dict[str, object]] = []

    async def resolve(self, **kwargs: object) -> PolicyRulesResolution:
        self.calls.append(kwargs)
        return self.resolution


def rules(**overrides: object) -> PolicyRules:
    values: dict[str, object] = {
        "requires_auth": True,
        "auth_criteria": list(CRITERIA),
        "step_therapy_required": False,
        "step_therapy_details": None,
    }
    values.update(overrides)
    return PolicyRules.model_validate(values)


def install(monkeypatch: pytest.MonkeyPatch, resolution: PolicyRulesResolution) -> Stage1:
    stage1 = Stage1(resolution)
    monkeypatch.setattr(policy_rules, "resolve_policy_rules", stage1.resolve)
    return stage1


async def ask(clinical_context: dict[str, object]) -> query.PolicyQueryAnswer:
    return await query.answer_policy_query(
        qdrant=object(),  # type: ignore[arg-type]  # Stage 1 is stubbed out
        redis=object(),  # type: ignore[arg-type]
        collection="insurance_policies",
        procedure="knee MRI",
        cpt_code="73721",
        payer="Aetna",
        plan_type="PPO",
        state="MA",
        clinical_context=clinical_context,
    )


async def test_the_answer_carries_both_halves(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, PolicyRulesResolution(rules=rules(), source="rag"))

    answer = await ask({"note": "Failed six weeks of conservative therapy"})

    assert answer.requires_auth is True
    assert answer.auth_criteria == CRITERIA
    assert answer.missing_criteria == ["Documented neurological deficit on examination"]
    assert answer.denial_risk == "medium"
    assert "knee MRI" in answer.nudge_message


async def test_the_clinical_context_never_reaches_stage_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not merely unused — unpassed. Stage 1's result is cached across patients."""
    stage1 = install(monkeypatch, PolicyRulesResolution(rules=rules(), source="rag"))

    await ask({"mrn": "MRN-8675309"})

    assert "clinical_context" not in stage1.calls[0]
    assert "MRN-8675309" not in str(stage1.calls[0])


async def test_stage_two_runs_even_on_a_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the second patient would inherit the first patient's gaps."""
    install(monkeypatch, PolicyRulesResolution(rules=rules(), source="cache"))

    first = await ask({"note": "Failed six weeks of conservative therapy"})
    second = await ask({"note": "Neurological deficit on examination"})

    assert first.auth_criteria == second.auth_criteria
    assert first.missing_criteria != second.missing_criteria


async def test_the_source_is_carried_for_the_log_but_not_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, PolicyRulesResolution(rules=rules(), source="cache"))

    answer = await ask({})

    assert answer.source == "cache"


async def test_step_therapy_passes_through_from_stage_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        PolicyRulesResolution(
            rules=rules(step_therapy_required=True, step_therapy_details="NSAIDs first"),
            source="rag",
        ),
    )

    answer = await ask({})

    assert answer.step_therapy_required is True
    assert answer.step_therapy_details == "NSAIDs first"


# --- the fallback ----------------------------------------------------------


async def test_a_fallback_resolution_returns_the_answer_task_012_specifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        PolicyRulesResolution(rules=policy_rules.FALLBACK_RULES, source="fallback"),
    )

    answer = await ask({"note": "Failed six weeks of conservative therapy"})

    assert answer.requires_auth is True
    assert answer.auth_criteria == []
    assert answer.missing_criteria == []
    assert answer.denial_risk == "high"
    assert answer.nudge_message == "Unable to verify authorization requirements — confirm manually"
    assert answer.step_therapy_required is False
    assert answer.step_therapy_details is None


async def test_the_fallback_does_not_compute_gaps_from_criteria_it_does_not_have(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``missing_criteria`` from a real answer means "nothing is missing".

    From a fallback it would mean "nothing is known", and the two are opposite
    advice wearing the same shape — which is why the risk is high and the
    message says so in words.
    """
    install(
        monkeypatch,
        PolicyRulesResolution(rules=policy_rules.FALLBACK_RULES, source="fallback"),
    )

    answer = await ask({})

    assert answer.missing_criteria == []
    assert answer.denial_risk == "high"
    assert "confirm manually" in answer.nudge_message


async def test_a_fallback_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install(
        monkeypatch,
        PolicyRulesResolution(rules=policy_rules.FALLBACK_RULES, source="fallback"),
    )

    with caplog.at_level("WARNING"):
        await ask({})

    assert "manual-review fallback" in caplog.text
    assert "73721" in caplog.text


async def test_a_log_line_names_the_plan_and_never_the_patient(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install(monkeypatch, PolicyRulesResolution(rules=rules(), source="rag"))

    with caplog.at_level("INFO"):
        await ask({"patient_name": "Zebulon Quackenbush", "mrn": "MRN-8675309"})

    assert "Aetna" in caplog.text
    assert "Quackenbush" not in caplog.text
    assert "8675309" not in caplog.text


def test_each_fallback_answer_owns_its_lists() -> None:
    """A shared module-level default would let one caller's mutation escape."""
    first = query.fallback_answer()
    second = query.fallback_answer()

    first.missing_criteria.append("mutated")

    assert second.missing_criteria == []

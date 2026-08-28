"""Generating a SOAP note and the codes alongside it.

The Bedrock calls are stubbed at :mod:`track_a_clinical.bedrock`'s two invokers,
so the answers can be chosen — which is the only way to test the retry, the
parsing and the two passes failing independently. ``test_bedrock.py`` covers the
clients themselves against moto.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import pytest

from track_a_clinical import bedrock, soap
from track_a_clinical.models import SOURCE_LLM_EXTRACTION

SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

TRANSCRIPT = (
    "Patient reports right knee pain for six weeks after a fall. "
    "Examination shows medial joint line tenderness. "
    "We will order an MRI of the right knee."
)

SOAP_ANSWER = json.dumps(
    {
        "subjective": "Right knee pain for six weeks following a fall.",
        "objective": "Medial joint line tenderness on the right.",
        "assessment": "Suspected medial meniscal tear, right knee.",
        "plan": "MRI right knee without contrast.",
    }
)

EXTRACTION_ANSWER = json.dumps(
    {
        "icd10_codes": [{"code": "M23.221", "display": "Derangement of medial meniscus"}],
        "cpt_codes": [{"code": "73721", "display": "MRI lower extremity without contrast"}],
    }
)


def _answers(*, soap_answer: str, extraction_answer: str) -> tuple[Callable, Callable]:
    """Return two invokers that reply with fixed answers and count their calls."""

    async def reasoning(prompt: str) -> str:
        reasoning.calls += 1  # type: ignore[attr-defined]
        return soap_answer

    async def fast(prompt: str) -> str:
        fast.calls += 1  # type: ignore[attr-defined]
        return extraction_answer

    reasoning.calls = 0  # type: ignore[attr-defined]
    fast.calls = 0  # type: ignore[attr-defined]
    return reasoning, fast


@pytest.fixture
def stub_bedrock(monkeypatch: pytest.MonkeyPatch) -> Callable[..., tuple[Callable, Callable]]:
    """Install a pair of stubbed invokers and hand them back for assertions."""

    def install(
        *, soap_answer: str = SOAP_ANSWER, extraction_answer: str = EXTRACTION_ANSWER
    ) -> tuple[Callable, Callable]:
        reasoning, fast = _answers(soap_answer=soap_answer, extraction_answer=extraction_answer)
        monkeypatch.setattr(bedrock, "invoke_reasoning", reasoning)
        monkeypatch.setattr(bedrock, "invoke_fast", fast)
        return reasoning, fast

    return install


# --- prompts ---------------------------------------------------------------


def test_the_soap_prompt_carries_the_transcript() -> None:
    assert TRANSCRIPT in soap.build_soap_prompt(TRANSCRIPT)


def test_the_extraction_prompt_carries_the_transcript() -> None:
    assert TRANSCRIPT in soap.build_extraction_prompt(TRANSCRIPT)


def test_the_extraction_prompt_forbids_a_self_reported_confidence() -> None:
    """The column has no home for one, and ExtractedCode rejects it outright."""
    assert "confidence" in soap.build_extraction_prompt(TRANSCRIPT)


def test_a_transcript_with_braces_does_not_break_prompt_formatting() -> None:
    """The prompt templates contain a JSON example, so they use doubled braces."""
    prompt = soap.build_soap_prompt("the patient said {something odd}")

    assert "{something odd}" in prompt
    assert '{"subjective"' in prompt


# --- parsing ---------------------------------------------------------------


def test_a_bare_soap_answer_parses() -> None:
    sections = soap.parse_soap(SOAP_ANSWER)

    assert sections is not None
    assert sections.assessment.startswith("Suspected medial meniscal tear")


def test_a_fenced_soap_answer_parses() -> None:
    """Models wrap JSON they were asked to return bare often enough to matter."""
    assert soap.parse_soap(f"```json\n{SOAP_ANSWER}\n```") is not None


def test_a_soap_answer_missing_a_section_is_unusable() -> None:
    answer = json.dumps({"subjective": "a", "objective": "b", "assessment": "c"})

    assert soap.parse_soap(answer) is None


def test_a_soap_answer_with_an_empty_section_is_unusable() -> None:
    """An empty string is a section the model declined to write, not a note."""
    answer = json.dumps({"subjective": "a", "objective": "b", "assessment": "c", "plan": ""})

    assert soap.parse_soap(answer) is None


def test_prose_with_no_json_in_it_is_unusable() -> None:
    assert soap.parse_soap("I am unable to produce a note from that.") is None


def test_a_truncated_answer_is_unusable() -> None:
    """A generation that hit the token ceiling: no balanced object to find."""
    assert soap.parse_soap('{"subjective": "a", ') is None


def test_a_balanced_but_invalid_document_is_unusable() -> None:
    """Braces match, so it is found — and then does not parse as JSON."""
    assert soap.parse_soap('{"subjective": }') is None


def test_extraction_entries_are_llm_sourced_and_unscored() -> None:
    parsed = soap.parse_extraction(EXTRACTION_ANSWER)

    assert parsed is not None
    icd10, cpt = parsed
    assert [entry.code for entry in icd10] == ["M23.221"]
    assert [entry.code for entry in cpt] == ["73721"]
    assert all(entry.source == SOURCE_LLM_EXTRACTION for entry in icd10 + cpt)
    assert all(entry.confidence is None for entry in icd10 + cpt)
    assert all(entry.validation is None for entry in icd10 + cpt)


def test_a_confidence_the_model_volunteered_is_dropped_not_stored() -> None:
    """It would otherwise be indistinguishable from Comprehend Medical's score."""
    answer = json.dumps({"icd10_codes": [{"code": "M17.11", "confidence": 0.99}], "cpt_codes": []})

    parsed = soap.parse_extraction(answer)

    assert parsed is not None
    assert parsed[0][0].confidence is None


def test_an_empty_extraction_is_a_valid_answer() -> None:
    """A visit where nothing codeable was discussed is an ordinary visit."""
    parsed = soap.parse_extraction(json.dumps({"icd10_codes": [], "cpt_codes": []}))

    assert parsed == ([], [])


def test_a_missing_code_list_defaults_to_empty() -> None:
    parsed = soap.parse_extraction(json.dumps({"icd10_codes": []}))

    assert parsed == ([], [])


def test_an_extraction_with_a_blank_code_is_unusable() -> None:
    """The whole answer, not one dropped entry — completeness is the point."""
    answer = json.dumps({"icd10_codes": [{"code": "   "}], "cpt_codes": []})

    assert soap.parse_extraction(answer) is None


def test_an_extraction_answer_of_the_wrong_shape_is_unusable() -> None:
    assert soap.parse_extraction(json.dumps({"icd10_codes": "M17.11"})) is None


def test_prose_with_no_json_is_an_unusable_extraction() -> None:
    assert soap.parse_extraction("no codes apply here") is None


# --- generation ------------------------------------------------------------


async def test_a_transcript_produces_a_note_and_its_codes(stub_bedrock: Callable) -> None:
    stub_bedrock()

    note = await soap.generate(TRANSCRIPT, session_id=SESSION_ID)

    assert note is not None
    assert note.sections.plan.startswith("MRI right knee")
    assert note.icd10_codes is not None
    assert [entry.code for entry in note.icd10_codes] == ["M23.221"]
    assert note.cpt_codes is not None
    assert [entry.code for entry in note.cpt_codes] == ["73721"]


async def test_the_two_passes_are_two_calls(stub_bedrock: Callable) -> None:
    """One call asked to do both would put extraction on the expensive model."""
    reasoning, fast = stub_bedrock()

    await soap.generate(TRANSCRIPT, session_id=SESSION_ID)

    assert reasoning.calls == 1
    assert fast.calls == 1


async def test_an_empty_transcript_generates_nothing(stub_bedrock: Callable) -> None:
    """A prompt with no transcript in it invites the model to invent an encounter."""
    reasoning, fast = stub_bedrock()

    assert await soap.generate("   ", session_id=SESSION_ID) is None
    assert reasoning.calls == 0
    assert fast.calls == 0


async def test_an_unusable_soap_answer_is_retried_once(stub_bedrock: Callable) -> None:
    reasoning, _ = stub_bedrock(soap_answer="not json at all")

    await soap.generate(TRANSCRIPT, session_id=SESSION_ID)

    assert reasoning.calls == soap.MAX_ATTEMPTS


async def test_a_soap_pass_that_never_parses_produces_no_note(stub_bedrock: Callable) -> None:
    """Nothing worth storing — the caller keeps its buffer and can retry."""
    stub_bedrock(soap_answer="not json at all")

    assert await soap.generate(TRANSCRIPT, session_id=SESSION_ID) is None


async def test_a_raising_soap_call_produces_no_note(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(prompt: str) -> str:
        raise RuntimeError("bedrock is unreachable")

    async def fast(prompt: str) -> str:
        return EXTRACTION_ANSWER

    monkeypatch.setattr(bedrock, "invoke_reasoning", boom)
    monkeypatch.setattr(bedrock, "invoke_fast", fast)

    assert await soap.generate(TRANSCRIPT, session_id=SESSION_ID) is None


async def test_a_failed_extraction_still_produces_the_note(stub_bedrock: Callable) -> None:
    """The note is what a provider reviews; withholding it loses more."""
    stub_bedrock(extraction_answer="not json at all")

    note = await soap.generate(TRANSCRIPT, session_id=SESSION_ID)

    assert note is not None
    assert note.sections.plan.startswith("MRI right knee")


async def test_a_failed_extraction_leaves_the_codes_unset_not_empty(
    stub_bedrock: Callable,
) -> None:
    """None records "never determined"; [] would claim the visit had no codes."""
    stub_bedrock(extraction_answer="not json at all")

    note = await soap.generate(TRANSCRIPT, session_id=SESSION_ID)

    assert note is not None
    assert note.icd10_codes is None
    assert note.cpt_codes is None


async def test_an_extraction_that_found_nothing_is_empty_not_unset(
    stub_bedrock: Callable,
) -> None:
    """The other half of the same distinction: this pass ran and found none."""
    stub_bedrock(extraction_answer=json.dumps({"icd10_codes": [], "cpt_codes": []}))

    note = await soap.generate(TRANSCRIPT, session_id=SESSION_ID)

    assert note is not None
    assert note.icd10_codes == []
    assert note.cpt_codes == []


async def test_neither_pass_logs_the_transcript(
    stub_bedrock: Callable, caplog: pytest.LogCaptureFixture
) -> None:
    """Everything here is PHI; log lines carry ids and counts, never speech."""
    stub_bedrock(soap_answer="not json at all", extraction_answer="not json either")

    with caplog.at_level("DEBUG", logger="track_a_clinical.soap"):
        await soap.generate(TRANSCRIPT, session_id=SESSION_ID)

    assert "knee pain" not in caplog.text
    assert "MRI" not in caplog.text
    assert str(SESSION_ID) in caplog.text

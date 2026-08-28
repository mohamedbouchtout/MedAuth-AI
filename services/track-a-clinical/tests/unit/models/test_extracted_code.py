"""The shape stored in clinical_notes.icd10_codes and cpt_codes.

Four consumers read or write this shape across three services, so the rules that
keep them agreeing are enforced in the model rather than left to call sites. See
CLAUDE.md "Extracted clinical codes — one JSON shape".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from track_a_clinical.models import (
    SOURCE_COMPREHEND_MEDICAL,
    SOURCE_LLM_EXTRACTION,
    CodeValidation,
    ExtractedCode,
    dump_codes,
    load_codes,
)


def test_the_haiku_pass_writes_an_unscored_unvalidated_entry() -> None:
    code = ExtractedCode.from_llm("M17.11", "Osteoarthritis, right knee")

    assert code.source == SOURCE_LLM_EXTRACTION
    assert code.confidence is None
    assert code.validation is None


def test_a_code_is_uppercased_on_write() -> None:
    """Matched by string equality downstream, so normalised where it is written."""
    assert ExtractedCode.from_llm(" m17.11 ").code == "M17.11"


def test_a_blank_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedCode.from_llm("   ")


def test_an_llm_extraction_may_not_carry_a_confidence() -> None:
    """A model's self-rating is not a measurement and must not share the column.

    Once a fabricated score sits next to Comprehend Medical's calibrated one,
    nothing downstream can tell which kind it is looking at.
    """
    with pytest.raises(ValidationError, match="self-rating"):
        ExtractedCode(code="M17.11", source=SOURCE_LLM_EXTRACTION, confidence=0.9)


def test_a_validating_source_may_carry_a_confidence() -> None:
    code = ExtractedCode(code="M17.11", source=SOURCE_COMPREHEND_MEDICAL, confidence=0.94)

    assert code.confidence == 0.94


@pytest.mark.parametrize("score", [-0.1, 1.1])
def test_a_confidence_outside_zero_to_one_is_rejected(score: float) -> None:
    with pytest.raises(ValidationError):
        ExtractedCode(code="M17.11", source=SOURCE_COMPREHEND_MEDICAL, confidence=score)


def test_an_unknown_source_is_rejected() -> None:
    """The vocabulary is closed: a third source is a decision, not a string."""
    with pytest.raises(ValidationError):
        ExtractedCode(code="M17.11", source="guesswork")  # type: ignore[arg-type]


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedCode(code="M17.11", source=SOURCE_LLM_EXTRACTION, note="extra")


def test_an_unconfirmed_code_is_not_the_same_as_an_unchecked_one() -> None:
    """`validation: null` means "not checked yet", never "checked and rejected".

    A code Comprehend Medical actively failed to find must be distinguishable
    from one written before TASK-031 existed — the same distinction CLAUDE.md
    draws between a payer's silence and its negative determination.
    """
    unchecked = ExtractedCode.from_llm("M17.11")
    rejected = ExtractedCode(
        code="M17.11",
        source=SOURCE_LLM_EXTRACTION,
        validation=CodeValidation(confidence=None, confirmed=False),
    )

    assert unchecked.validation is None
    assert rejected.validation is not None
    assert rejected.validation.confirmed is False


def test_a_validation_defaults_to_naming_comprehend_medical() -> None:
    assert CodeValidation(confirmed=True).source == SOURCE_COMPREHEND_MEDICAL


def test_dumping_produces_json_scalars_only() -> None:
    """asyncpg encodes the JSONB column from this; a Python object fails there."""
    dumped = dump_codes([ExtractedCode.from_llm("M17.11", "Knee OA")])

    assert dumped == [
        {
            "code": "M17.11",
            "display": "Knee OA",
            "source": SOURCE_LLM_EXTRACTION,
            "confidence": None,
            "validation": None,
        }
    ]


def test_a_round_trip_through_the_column_preserves_the_entry() -> None:
    codes = [
        ExtractedCode.from_llm("M17.11"),
        ExtractedCode(
            code="73721",
            source=SOURCE_COMPREHEND_MEDICAL,
            confidence=0.88,
            validation=CodeValidation(confidence=0.88, confirmed=True),
        ),
    ]

    assert load_codes(dump_codes(codes)) == codes


def test_an_empty_column_loads_as_no_codes() -> None:
    """Both columns are nullable: a transcript naming no diagnosis has none."""
    assert load_codes(None) == []


def test_a_column_holding_something_other_than_an_array_is_rejected() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        load_codes({"code": "M17.11"})

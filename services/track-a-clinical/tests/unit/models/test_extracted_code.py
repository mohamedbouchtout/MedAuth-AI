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
    matching_key,
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


# --- dot normalisation (TASK-031) -------------------------------------------


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        ("M17.11", "M17.11"),
        ("M1711", "M17.11"),
        (" m17.11 ", "M17.11"),
        ("e119", "E11.9"),
        ("S52501A", "S52.501A"),
        ("Z0000", "Z00.00"),
    ],
)
def test_an_icd10_code_is_stored_dotted_however_it_was_written(written: str, stored: str) -> None:
    """One diagnosis must not become two entries because two sources dot it differently.

    Uppercasing alone left ``M1711`` and ``M17.11`` as separate strings, which
    is the payer-slug bug one column over: a silent mismatch that looks like a
    code the other source never proposed.
    """
    assert ExtractedCode.from_llm(written).code == stored


def test_a_three_character_icd10_code_takes_no_dot() -> None:
    """``I10`` has no extension, so there is nothing to put a dot in front of."""
    assert ExtractedCode.from_llm("I10").code == "I10"


def test_a_cpt_code_is_never_dotted() -> None:
    """Five digits is not an ICD-10 shape, and inserting a dot would corrupt it."""
    assert ExtractedCode.from_llm("73721").code == "73721"


def test_both_icd10_spellings_share_one_matching_key() -> None:
    """UNVERIFIED — see :func:`track_a_clinical.models.matching_key`.

    Which spelling AWS Comprehend Medical returns has never been checked against
    the live service and cannot be settled from the API contract. Reducing both
    to one key is what makes the comparison correct under either answer.
    """
    assert matching_key("M17.11") == matching_key("M1711") == "M1711"


def test_the_matching_key_is_case_and_whitespace_insensitive() -> None:
    assert matching_key(" m17.11 ") == "M1711"


def test_a_blank_code_is_still_rejected() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        ExtractedCode.from_llm("   ")


# --- codes Comprehend proposed on its own (TASK-030) -------------------------


def test_a_comprehend_proposal_carries_a_real_score_and_no_validation() -> None:
    """The score is Comprehend's own; nothing independent has weighed in on it.

    ``validation`` stays ``None`` permanently on these entries — validating a
    code against the source that proposed it is the same circularity that keeps
    the validation pass reading the transcript rather than the generated note.
    """
    code = ExtractedCode.from_comprehend("M1711", "Synthetic knee description", 0.93)

    assert code.code == "M17.11"
    assert code.display == "Synthetic knee description"
    assert code.source == SOURCE_COMPREHEND_MEDICAL
    assert code.confidence == pytest.approx(0.93)
    assert code.validation is None


def test_a_comprehend_proposal_with_no_description_keeps_none() -> None:
    """``display`` is the source's own words or nothing — never invented."""
    assert ExtractedCode.from_comprehend("I10", None, 0.97).display is None


def test_a_comprehend_proposal_is_distinguishable_from_an_llm_one() -> None:
    """More than a label: an llm-extraction entry cannot structurally hold a score."""
    proposed = ExtractedCode.from_comprehend("I10", None, 0.97)
    extracted = ExtractedCode.from_llm("I10")

    assert proposed.source != extracted.source
    assert proposed.confidence is not None
    assert extracted.confidence is None

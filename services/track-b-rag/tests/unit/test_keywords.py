"""Procedure keyword detection and the excerpt that travels with a match."""

from __future__ import annotations

import pytest

from track_b_rag.keywords import detect_procedures, extract_context

# TASK-021 fixes this list; the parametrisation is here so a keyword cannot be
# dropped from the module without a test going red.
EXPECTED_KEYWORDS = [
    ("Let's order an MRI of that knee.", "MRI"),
    ("We should get a CT scan first.", "CT scan"),
    ("I want an X-ray of the wrist.", "X-ray"),
    ("We'll do a biopsy of the lesion.", "biopsy"),
    ("A cortisone injection should help.", "injection"),
    ("She may need an arthroscopy.", "arthroscopy"),
    ("Let's get an echocardiogram.", "echocardiogram"),
    ("Schedule a stress test.", "stress test"),
    ("He is a candidate for a biologic.", "biologic"),
    ("We are starting chemotherapy.", "chemotherapy"),
]


@pytest.mark.parametrize(("text", "keyword"), EXPECTED_KEYWORDS)
def test_each_keyword_in_the_list_is_detected(text: str, keyword: str) -> None:
    (mention,) = detect_procedures(text)

    assert mention.keyword == keyword
    assert mention.procedure == keyword


@pytest.mark.parametrize(
    "text",
    ["let's order an mri", "LET'S ORDER AN MRI", "Order MRIs for both knees"],
)
def test_detection_is_case_insensitive_and_reads_plurals(text: str) -> None:
    (mention,) = detect_procedures(text)

    assert mention.keyword == "MRI"


@pytest.mark.parametrize("spelling", ["x-ray", "x ray", "xray", "X-Ray"])
def test_the_x_ray_spellings_are_one_keyword(spelling: str) -> None:
    """Transcribe writes it differently depending on how it was said."""
    (mention,) = detect_procedures(f"Get an {spelling} of the wrist.")

    assert mention.keyword == "X-ray"


@pytest.mark.parametrize(
    "text",
    [
        "The CTA showed no occlusion.",
        "Her medications are unchanged.",
        "We discussed physical therapy.",
    ],
)
def test_text_with_no_procedure_yields_nothing(text: str) -> None:
    assert detect_procedures(text) == []


def test_a_referral_carries_the_specialist() -> None:
    """A bare "referral" would not be enough to ask a payer about."""
    (mention,) = detect_procedures("I'll put in a referral to orthopedics.")

    assert mention.keyword == "referral"
    assert mention.procedure == "referral to orthopedics"


@pytest.mark.parametrize(
    ("text", "procedure"),
    [
        ("Referral to a rheumatologist, please.", "referral to rheumatologist"),
        ("A referral to the pain clinic.", "referral to pain clinic"),
        ("Referral to Sports Medicine today.", "referral to sports medicine"),
    ],
)
def test_the_referral_article_is_dropped_and_the_specialist_kept(text: str, procedure: str) -> None:
    (mention,) = detect_procedures(text)

    assert mention.procedure == procedure


def test_one_mention_per_keyword_per_segment() -> None:
    """Two policy queries for one sentence would cost twice and say the same thing."""
    mentions = detect_procedures("An MRI. Really, an MRI, and then another MRI.")

    assert [mention.keyword for mention in mentions] == ["MRI"]


def test_several_distinct_keywords_come_back_in_the_order_they_appear() -> None:
    mentions = detect_procedures("Start with an X-ray, then an MRI, then maybe a biopsy.")

    assert [mention.keyword for mention in mentions] == ["X-ray", "MRI", "biopsy"]


def test_a_match_is_a_candidate_not_a_determination() -> None:
    """Hedged speech still queries — the policy answer decides, not this module."""
    (mention,) = detect_procedures("We could do an MRI, but let's wait and see.")

    assert mention.keyword == "MRI"


def test_the_excerpt_carries_the_sentence_before_the_keyword() -> None:
    """The indication is usually in the sentence before the order."""
    text = (
        "The knee has been locking for three months. Conservative therapy failed. "
        "Let's order an MRI."
    )

    (mention,) = detect_procedures(text)

    assert mention.excerpt == "Conservative therapy failed. Let's order an MRI."


def test_the_excerpt_is_the_whole_segment_when_there_is_no_punctuation() -> None:
    """Falling back to everything beats falling back to nothing."""
    text = "the knee has been locking and I want an MRI"

    (mention,) = detect_procedures(text)

    assert mention.excerpt == text


def test_a_keyword_in_the_first_sentence_carries_only_that_sentence() -> None:
    text = "Let's order an MRI. We'll review it next week."

    (mention,) = detect_procedures(text)

    assert mention.excerpt == "Let's order an MRI."


def test_context_beyond_the_end_of_the_text_falls_back_to_the_last_sentence() -> None:
    """A position past the end can only be a caller bug; answer usefully anyway."""
    text = "First sentence. Second sentence."

    assert extract_context(text, 10_000) == "First sentence. Second sentence."


def test_context_of_empty_text_is_empty() -> None:
    assert extract_context("   ", 0) == ""


def test_the_matched_text_is_what_actually_matched() -> None:
    (mention,) = detect_procedures("Order X-Rays of both hands.")

    assert mention.matched_text == "X-Rays"

"""Validating the LLM's ICD-10 codes against Comprehend Medical (TASK-031).

The service call is stubbed at :func:`track_a_clinical.comprehend._infer_icd10`,
because moto does not implement Comprehend Medical and a live ``@mock_aws`` call
to ``InferICD10CM`` returns ``404 Not yet implemented`` — see CLAUDE.md's
standing exception, and :mod:`tests.unit.comprehend_fixtures` for what is
faithful about the responses used here and what is not.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from tests.unit import comprehend_fixtures as fixtures
from track_a_clinical import comprehend
from track_a_clinical.config import get_settings
from track_a_clinical.models import (
    SOURCE_COMPREHEND_MEDICAL,
    ExtractedCode,
    matching_key,
)

TRANSCRIPT = (
    "Patient reports right knee pain and stiffness. History of type 2 diabetes "
    "and essential hypertension, both stable on current therapy."
)


def _stub_inference(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[dict[str, Any]] | dict[str, Any],
    *,
    calls: list[str] | None = None,
) -> None:
    """Answer each ``_infer_icd10`` call from `responses`, in order."""
    queue = responses if isinstance(responses, list) else [responses]

    async def fake(text: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(text)
        return queue[min(len(calls or []) - 1 if calls else 0, len(queue) - 1)]

    monkeypatch.setattr(comprehend, "_infer_icd10", fake)


@pytest.mark.asyncio
async def test_three_clear_diagnoses_are_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK-031's headline test: three clear diagnoses, verify codes match."""
    _stub_inference(monkeypatch, fixtures.THREE_CLEAR_DIAGNOSES)
    codes = [
        ExtractedCode.from_llm("M17.11"),
        ExtractedCode.from_llm("E11.9"),
        ExtractedCode.from_llm("I10"),
    ]

    validated = await comprehend.validate_icd10(codes, TRANSCRIPT)

    assert [entry.code for entry in validated] == ["M17.11", "E11.9", "I10"]
    for entry in validated:
        assert entry.validation is not None
        assert entry.validation.confirmed is True
        assert entry.validation.source == SOURCE_COMPREHEND_MEDICAL


@pytest.mark.asyncio
async def test_a_dotless_comprehend_code_still_matches_a_dotted_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNVERIFIED — the real spelling is unknown, so both must match.

    ``E119`` in the fixture and ``E11.9`` from the LLM are one code. Which
    spelling AWS actually returns has never been checked against the live
    service and cannot be settled from botocore's contract, so the comparison is
    built to be correct under either answer. If this ever starts depending on
    one spelling, this test fails — which is the point of it.
    """
    _stub_inference(monkeypatch, fixtures.THREE_CLEAR_DIAGNOSES)

    validated = await comprehend.validate_icd10([ExtractedCode.from_llm("E11.9")], TRANSCRIPT)

    assert validated[0].validation is not None
    assert validated[0].validation.confirmed is True
    assert validated[0].validation.confidence == pytest.approx(0.91)


@pytest.mark.asyncio
async def test_a_code_comprehend_never_returned_is_unconfirmed_with_no_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not seen at all: ``confirmed`` false, ``confidence`` None — never null validation.

    ``validation: None`` means "not checked yet". A code the validator actively
    failed to find has been checked, and collapsing the two would make it
    indistinguishable from a note written before this task existed.
    """
    _stub_inference(monkeypatch, fixtures.NOTHING_FOUND)

    validated = await comprehend.validate_icd10([ExtractedCode.from_llm("M17.11")], TRANSCRIPT)

    assert validated[0].validation is not None
    assert validated[0].validation.confirmed is False
    assert validated[0].validation.confidence is None


@pytest.mark.asyncio
async def test_a_low_scoring_code_keeps_its_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen but unsure is a different fact from not seen, and both are recorded."""
    _stub_inference(monkeypatch, fixtures.WEAKLY_LINKED)

    validated = await comprehend.validate_icd10([ExtractedCode.from_llm("M17.11")], TRANSCRIPT)

    assert validated[0].validation is not None
    assert validated[0].validation.confirmed is False
    assert validated[0].validation.confidence == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_the_concept_score_is_read_and_not_the_entity_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confidently-detected entity linked weakly to a code is not confirmed.

    The fixture's entity ``Score`` is 0.99 and its concept ``Score`` is 0.42.
    Reading the entity score would confirm a code Comprehend was in fact unsure
    about — exactly the error this validation exists to catch.
    """
    _stub_inference(monkeypatch, fixtures.WEAKLY_LINKED)

    validated = await comprehend.validate_icd10([ExtractedCode.from_llm("M17.11")], TRANSCRIPT)

    assert validated[0].validation is not None
    assert validated[0].validation.confidence == pytest.approx(0.42)
    assert validated[0].validation.confirmed is False


@pytest.mark.asyncio
async def test_a_comprehend_failure_leaves_every_code_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation is never fatal: the codes come back exactly as they arrived."""

    async def boom(text: str) -> dict[str, Any]:
        raise RuntimeError("throttled")

    monkeypatch.setattr(comprehend, "_infer_icd10", boom)
    codes = [ExtractedCode.from_llm("M17.11")]

    validated = await comprehend.validate_icd10(codes, TRANSCRIPT)

    assert validated is codes
    assert validated[0].validation is None


@pytest.mark.asyncio
async def test_a_failure_is_logged_without_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The chunk is encounter speech and must never reach a log line."""

    async def boom(text: str) -> dict[str, Any]:
        raise RuntimeError("throttled")

    monkeypatch.setattr(comprehend, "_infer_icd10", boom)

    with caplog.at_level(logging.WARNING):
        await comprehend.validate_icd10([ExtractedCode.from_llm("M17.11")], TRANSCRIPT)

    assert caplog.text
    assert "knee pain" not in caplog.text
    assert "diabetes" not in caplog.text


@pytest.mark.asyncio
async def test_an_unconfirmed_code_is_logged_with_its_score_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Discrepancies are a quality metric: the code and the score, no clinical text."""
    _stub_inference(monkeypatch, fixtures.WEAKLY_LINKED)

    with caplog.at_level(logging.WARNING):
        await comprehend.validate_icd10([ExtractedCode.from_llm("M17.11")], TRANSCRIPT)

    assert "M17.11" in caplog.text
    assert "0.420" in caplog.text
    assert "knee pain" not in caplog.text


@pytest.mark.asyncio
async def test_a_comprehend_sourced_entry_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This pass checks what the LLM proposed and leaves anything else alone."""
    _stub_inference(monkeypatch, fixtures.NOTHING_FOUND)
    entry = ExtractedCode(code="M17.11", source=SOURCE_COMPREHEND_MEDICAL, confidence=0.9)

    validated = await comprehend.validate_icd10([entry], TRANSCRIPT)

    assert validated[0].validation is None


@pytest.mark.asyncio
async def test_no_codes_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing to validate means no request is made at all."""
    calls: list[str] = []
    _stub_inference(monkeypatch, fixtures.NOTHING_FOUND, calls=calls)

    assert await comprehend.validate_icd10([], TRANSCRIPT) == []
    assert calls == []


@pytest.mark.asyncio
async def test_an_empty_transcript_leaves_codes_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No text to validate against is "not checked", not "not confirmed"."""
    calls: list[str] = []
    _stub_inference(monkeypatch, fixtures.NOTHING_FOUND, calls=calls)
    codes = [ExtractedCode.from_llm("M17.11")]

    validated = await comprehend.validate_icd10(codes, "   ")

    assert validated is codes
    assert validated[0].validation is None
    assert calls == []


# --- chunking ---------------------------------------------------------------


def test_a_short_transcript_is_one_chunk() -> None:
    assert comprehend.split_for_requests("a b c") == ["a b c"]


def test_an_empty_transcript_is_no_chunks() -> None:
    assert comprehend.split_for_requests("   ") == []


def test_chunks_stay_within_the_limit() -> None:
    text = " ".join(["word"] * 5_000)
    chunks = comprehend.split_for_requests(text, limit=100)
    assert all(len(chunk) <= 100 for chunk in chunks)


def test_chunking_discards_nothing() -> None:
    """The property that separates chunking from the truncation CLAUDE.md forbids."""
    text = " ".join(f"w{index}" for index in range(2_000))
    chunks = comprehend.split_for_requests(text, limit=97)
    assert " ".join(chunks).split() == text.split()


def test_a_word_is_never_split_across_chunks() -> None:
    chunks = comprehend.split_for_requests("alpha beta gamma delta", limit=11)
    for chunk in chunks:
        for word in chunk.split():
            assert word in {"alpha", "beta", "gamma", "delta"}


def test_a_single_oversized_token_is_emitted_whole() -> None:
    """Reported as uncovered rather than cut. Speech does not produce this."""
    chunks = comprehend.split_for_requests("x" * 50, limit=10)
    assert chunks == ["x" * 50]


@pytest.mark.asyncio
async def test_a_long_transcript_is_chunked_and_results_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code confirmed only in a later chunk is confirmed in the result."""
    calls: list[str] = []
    responses = [fixtures.NOTHING_FOUND, fixtures.THREE_CLEAR_DIAGNOSES]

    async def fake(text: str) -> dict[str, Any]:
        calls.append(text)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(comprehend, "_infer_icd10", fake)
    long_transcript = " ".join(["word"] * 4_000)
    assert len(long_transcript) > comprehend.MAX_REQUEST_CHARACTERS

    validated = await comprehend.validate_icd10([ExtractedCode.from_llm("M17.11")], long_transcript)

    assert len(calls) > 1
    assert validated[0].validation is not None
    assert validated[0].validation.confirmed is True


@pytest.mark.asyncio
async def test_a_partly_failed_validation_says_so(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Incomplete coverage is reported, never presented as a complete check."""
    calls: list[str] = []

    async def fake(text: str) -> dict[str, Any]:
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("throttled")
        return fixtures.THREE_CLEAR_DIAGNOSES

    monkeypatch.setattr(comprehend, "_infer_icd10", fake)
    long_transcript = " ".join(["word"] * 4_000)

    with caplog.at_level(logging.WARNING):
        await comprehend.validate_icd10([ExtractedCode.from_llm("M17.11")], long_transcript)

    assert "incomplete" in caplog.text.lower()


@pytest.mark.asyncio
async def test_an_oversized_chunk_is_reported_and_not_sent(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A token past the request limit is named as uncovered, never truncated."""
    calls: list[str] = []
    _stub_inference(monkeypatch, fixtures.THREE_CLEAR_DIAGNOSES, calls=calls)
    monkeypatch.setattr(comprehend, "MAX_REQUEST_CHARACTERS", 10)

    with caplog.at_level(logging.WARNING):
        await comprehend.validate_icd10([ExtractedCode.from_llm("M17.11")], "x" * 50)

    assert calls == []
    assert "exceed" in caplog.text.lower()


# --- the response reader ----------------------------------------------------


def test_best_scores_keeps_the_highest_per_code() -> None:
    """One condition named twice is judged on the mention read most clearly."""
    response = fixtures.response(
        fixtures.entity("knee pain", concepts=[("M17.11", 0.40)]),
        fixtures.entity("right knee osteoarthritis", concepts=[("M17.11", 0.93)]),
    )
    assert comprehend._best_scores(response)[matching_key("M17.11")] == pytest.approx(0.93)


def test_best_scores_ignores_a_concept_with_no_score() -> None:
    response = {"Entities": [{"ICD10CMConcepts": [{"Code": "M17.11"}]}]}
    assert comprehend._best_scores(response) == {}


def test_best_scores_ignores_a_concept_with_no_code() -> None:
    response = {"Entities": [{"ICD10CMConcepts": [{"Score": 0.9}]}]}
    assert comprehend._best_scores(response) == {}


def test_best_scores_tolerates_an_empty_response() -> None:
    assert comprehend._best_scores({}) == {}


# --- the client -------------------------------------------------------------


def test_the_client_is_built_once_in_the_configured_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cached like the Bedrock clients, for the same reason: one credential resolution.

    Not built under ``@mock_aws``: moto has no Comprehend Medical backend at
    all, so there is nothing for it to stand in for here. See this module's
    docstring.
    """
    monkeypatch.setenv("JWT_SIGNING_KEY", "a" * 32)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    get_settings.cache_clear()
    built: list[tuple[str, str]] = []

    def fake_client(service: str, region_name: str) -> object:
        built.append((service, region_name))
        return object()

    monkeypatch.setattr(comprehend.boto3, "client", fake_client)
    comprehend.reset_client()

    first = comprehend.get_client()
    second = comprehend.get_client()

    assert first is second
    assert built == [("comprehendmedical", "us-east-1")]
    comprehend.reset_client()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_the_sync_call_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """boto3 is synchronous; blocking here would stall every other live encounter.

    Asserts the call lands on a worker thread rather than the loop's thread —
    which is what ``asyncio.to_thread`` buys and what a direct call would lose.
    """
    import threading

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    class FakeClient:
        def infer_icd10_cm(self, Text: str) -> dict[str, Any]:  # noqa: N803
            seen["thread"] = threading.get_ident()
            return fixtures.NOTHING_FOUND

    monkeypatch.setattr(comprehend, "get_client", lambda: FakeClient())

    await comprehend._infer_icd10("some text")

    assert seen["thread"] != loop_thread

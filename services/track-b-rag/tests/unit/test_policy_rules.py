"""Stage 1: the cache, the model call, the one retry, and the safe fallback.

Three claims TASK-012 makes are settled here rather than at the HTTP level,
where they would be harder to see: a cache hit reaches neither Qdrant nor
Bedrock, an unparseable answer is retried exactly once, and a fallback is never
written to the cache. The last is the one with teeth — a cached fallback would
answer "confirm manually" for a day for every patient on that plan, and would
look like a working cache while doing it.

The signature is itself an assertion. :func:`resolve_policy_rules` takes no
clinical context, so nothing patient-specific can reach the prompt or the cached
value; ``test_stage_one_cannot_see_a_patient`` states that explicitly so a later
edit adding such a parameter fails a test rather than passing review.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from track_b_rag import bedrock, cache, policy_rules, retrieval
from track_b_rag.policy_rules import PolicyRules, resolve_policy_rules

ANSWER = {
    "requires_auth": True,
    "auth_criteria": ["Failed six weeks of conservative therapy"],
    "step_therapy_required": False,
    "step_therapy_details": None,
}

CHUNKS = [
    retrieval.RetrievedChunk(text="Prior authorization criteria", policy_id="L33575", score=0.9)
]

KEY = "rag:Aetna:PPO:MA:73721"


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expiries: dict[str, int | None] = {}

    async def get(self, key: str) -> bytes | None:
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        self.expiries[key] = ex
        return True


class Recorder:
    """Stands in for retrieval and Bedrock, counting what each was asked to do."""

    def __init__(self) -> None:
        self.retrievals = 0
        self.prompts: list[str] = []
        self.answers: list[str | Exception] = [json.dumps(ANSWER)]
        self.chunks: list[retrieval.RetrievedChunk] = list(CHUNKS)
        self.retrieval_error: Exception | None = None

    def retrieve(self, client: object, **kwargs: Any) -> list[retrieval.RetrievedChunk]:
        self.retrievals += 1
        if self.retrieval_error is not None:
            raise self.retrieval_error
        return self.chunks

    async def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self.answers) - 1)
        answer = self.answers[index]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    captured = Recorder()
    monkeypatch.setattr(retrieval, "retrieve", captured.retrieve)
    monkeypatch.setattr(bedrock, "invoke_reasoning", captured.invoke)
    return captured


async def resolve(redis: FakeRedis) -> policy_rules.PolicyRulesResolution:
    """Resolve the one query every test in this module asks about."""
    return await resolve_policy_rules(
        qdrant=object(),  # type: ignore[arg-type]  # the fake retrieval never uses it
        redis=redis,  # type: ignore[arg-type]  # only get/set are exercised
        collection="insurance_policies",
        procedure="knee MRI",
        cpt_code="73721",
        payer="Aetna",
        plan_type="PPO",
        state="MA",
    )


# --- the happy path --------------------------------------------------------


async def test_a_miss_retrieves_asks_bedrock_and_caches(
    redis: FakeRedis, recorder: Recorder
) -> None:
    resolution = await resolve(redis)

    assert resolution.source == "rag"
    assert resolution.rules.auth_criteria == ["Failed six weeks of conservative therapy"]
    assert recorder.retrievals == 1
    assert len(recorder.prompts) == 1
    assert json.loads(redis.store[KEY])["auth_criteria"] == resolution.rules.auth_criteria


async def test_the_cached_entry_carries_the_24h_ttl(redis: FakeRedis, recorder: Recorder) -> None:
    await resolve(redis)

    assert redis.expiries[KEY] == cache.POLICY_RULES_TTL_SECONDS


async def test_a_hit_touches_neither_qdrant_nor_bedrock(
    redis: FakeRedis, recorder: Recorder
) -> None:
    """The whole point of the cache: the expensive half is paid for once."""
    await resolve(redis)
    recorder.retrievals = 0
    recorder.prompts.clear()

    resolution = await resolve(redis)

    assert resolution.source == "cache"
    assert recorder.retrievals == 0
    assert recorder.prompts == []


async def test_the_cached_value_round_trips_every_field(
    redis: FakeRedis, recorder: Recorder
) -> None:
    recorder.answers = [
        json.dumps(
            {
                "requires_auth": True,
                "auth_criteria": ["A", "B"],
                "step_therapy_required": True,
                "step_therapy_details": "NSAIDs first",
            }
        )
    ]

    first = await resolve(redis)
    second = await resolve(redis)

    assert second.source == "cache"
    assert second.rules == first.rules


async def test_the_prompt_carries_the_retrieved_passages_and_the_code(
    redis: FakeRedis, recorder: Recorder
) -> None:
    await resolve(redis)

    prompt = recorder.prompts[0]
    assert "Prior authorization criteria" in prompt
    assert "73721" in prompt
    assert "L33575" in prompt


# --- the retry and the fallback --------------------------------------------


async def test_an_unparseable_answer_is_retried_once(redis: FakeRedis, recorder: Recorder) -> None:
    recorder.answers = ["not json at all", json.dumps(ANSWER)]

    resolution = await resolve(redis)

    assert resolution.source == "rag"
    assert len(recorder.prompts) == 2
    assert recorder.prompts[0] == recorder.prompts[1]  # the same prompt, per TASK-012


async def test_two_unparseable_answers_fall_back(
    redis: FakeRedis, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    recorder.answers = ["not json", "still not json"]

    with caplog.at_level("WARNING"):
        resolution = await resolve(redis)

    assert resolution.source == "fallback"
    assert resolution.rules == policy_rules.FALLBACK_RULES
    assert len(recorder.prompts) == 2


async def test_the_fallback_is_never_cached(redis: FakeRedis, recorder: Recorder) -> None:
    """A cached fallback would answer "confirm manually" for a day, for everyone."""
    recorder.answers = ["not json", "still not json"]

    await resolve(redis)

    assert redis.store == {}


async def test_the_fallback_assumes_authorization_is_required() -> None:
    """Failing the other way — assuming none is needed — is the expensive mistake."""
    assert policy_rules.FALLBACK_RULES.requires_auth is True
    assert policy_rules.FALLBACK_RULES.auth_criteria == []


async def test_json_of_the_wrong_shape_counts_as_unparseable(
    redis: FakeRedis, recorder: Recorder
) -> None:
    recorder.answers = [json.dumps({"unexpected": "shape"}), json.dumps({"also": "wrong"})]

    assert (await resolve(redis)).source == "fallback"


async def test_a_bedrock_error_is_retried_then_falls_back(
    redis: FakeRedis, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """An invocation that never returns is the same failure as one that returns junk."""
    recorder.answers = [RuntimeError("bedrock is unavailable")]

    with caplog.at_level("ERROR"):
        resolution = await resolve(redis)

    assert resolution.source == "fallback"
    assert len(recorder.prompts) == 2
    assert redis.store == {}


async def test_a_bedrock_error_then_a_good_answer_succeeds(
    redis: FakeRedis, recorder: Recorder
) -> None:
    recorder.answers = [RuntimeError("transient"), json.dumps(ANSWER)]

    assert (await resolve(redis)).source == "rag"


async def test_an_unreachable_qdrant_falls_back_without_calling_bedrock(
    redis: FakeRedis, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    recorder.retrieval_error = ConnectionError("qdrant is down")

    with caplog.at_level("ERROR"):
        resolution = await resolve(redis)

    assert resolution.source == "fallback"
    assert recorder.prompts == []
    assert "Policy retrieval failed" in caplog.text


async def test_nothing_indexed_falls_back_without_calling_bedrock(
    redis: FakeRedis, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """Asking Sonnet to answer from no policy text at all is an invitation to invent one."""
    recorder.chunks = []

    with caplog.at_level("WARNING"):
        resolution = await resolve(redis)

    assert resolution.source == "fallback"
    assert recorder.prompts == []
    assert "No indexed policy text matched" in caplog.text


# --- logging ---------------------------------------------------------------


async def test_a_failure_logs_the_payer_and_the_code(
    redis: FakeRedis, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    recorder.answers = ["not json", "still not json"]

    with caplog.at_level("WARNING"):
        await resolve(redis)

    assert "Aetna" in caplog.text
    assert "73721" in caplog.text
    assert "knee MRI" in caplog.text


# --- the cache entry itself ------------------------------------------------


async def test_a_corrupt_cache_entry_is_treated_as_a_miss(
    redis: FakeRedis, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """Recomputing is always available; a cache that can poison a request is not."""
    redis.store[KEY] = "{not json"

    with caplog.at_level("WARNING"):
        resolution = await resolve(redis)

    assert resolution.source == "rag"
    assert "unreadable cache entry" in caplog.text


async def test_an_entry_of_an_older_shape_is_treated_as_a_miss(
    redis: FakeRedis, recorder: Recorder
) -> None:
    redis.store[KEY] = json.dumps({"requires_auth": "maybe"})

    assert (await resolve(redis)).source == "rag"


# --- parsing ---------------------------------------------------------------


def test_a_fenced_answer_still_parses() -> None:
    """Models fence JSON even when told not to."""
    parsed = policy_rules.parse_rules(f"```json\n{json.dumps(ANSWER)}\n```")

    assert parsed is not None
    assert parsed.requires_auth is True


def test_a_prefaced_answer_still_parses() -> None:
    parsed = policy_rules.parse_rules(
        f"Here is the analysis:\n{json.dumps(ANSWER)}\nHope it helps."
    )

    assert parsed is not None


def test_braces_inside_a_criterion_do_not_truncate_the_object() -> None:
    """The scan tracks string literals, so a brace in the text is just a character."""
    answer = json.dumps(
        {
            "requires_auth": True,
            "auth_criteria": ["Documented {sic} deficit"],
            "step_therapy_required": False,
            "step_therapy_details": None,
        }
    )

    parsed = policy_rules.parse_rules(answer)

    assert parsed is not None
    assert parsed.auth_criteria == ["Documented {sic} deficit"]


def test_an_escaped_quote_inside_a_criterion_survives() -> None:
    answer = '{"requires_auth": true, "auth_criteria": ["say \\"ah\\" first"]}'

    parsed = policy_rules.parse_rules(answer)

    assert parsed is not None
    assert parsed.auth_criteria == ['say "ah" first']


def test_prose_with_no_json_at_all_does_not_parse() -> None:
    assert policy_rules.parse_rules("I could not determine the requirements.") is None


def test_a_balanced_span_that_is_not_json_does_not_parse() -> None:
    """Braces alone do not make a document — the model can emit pseudo-JSON."""
    assert policy_rules.parse_rules("{requires_auth: yes, criteria: none}") is None


def test_an_unterminated_object_does_not_parse() -> None:
    assert policy_rules.parse_rules('{"requires_auth": true') is None


def test_a_json_array_is_not_an_answer() -> None:
    assert policy_rules.parse_rules("[1, 2, 3]") is None


def test_an_extra_key_does_not_cost_the_retry() -> None:
    """A usable answer with a chatty extra field is still a usable answer."""
    parsed = policy_rules.parse_rules(json.dumps({**ANSWER, "notes": "see page 4"}))

    assert parsed is not None


def test_blank_criteria_are_dropped() -> None:
    """Stage 2 could only ever report an empty criterion as missing."""
    parsed = policy_rules.parse_rules(
        json.dumps({"requires_auth": True, "auth_criteria": ["  ", "Real criterion", ""]})
    )

    assert parsed is not None
    assert parsed.auth_criteria == ["Real criterion"]


def test_blank_step_therapy_details_read_as_absent() -> None:
    parsed = policy_rules.parse_rules(
        json.dumps({"requires_auth": True, "step_therapy_details": "   "})
    )

    assert parsed is not None
    assert parsed.step_therapy_details is None


def test_the_retry_budget_is_two_attempts() -> None:
    """TASK-012 says one retry. Two attempts total is what that means."""
    assert policy_rules.MAX_ATTEMPTS == 2


# --- the structural guarantee ----------------------------------------------


def test_stage_one_cannot_see_a_patient() -> None:
    """The cached half takes no clinical context, by signature and not by habit.

    Adding such a parameter would let patient-specific input influence a value
    that is then shared with every other patient on the plan. This test is what
    makes that a build failure rather than a review comment.
    """
    parameters = set(inspect.signature(resolve_policy_rules).parameters)

    assert "clinical_context" not in parameters
    assert parameters == {
        "qdrant",
        "redis",
        "collection",
        "procedure",
        "cpt_code",
        "payer",
        "plan_type",
        "state",
    }


def test_the_cached_model_holds_only_payer_policy_fields() -> None:
    """The patient-specific three are absent from what gets written to Redis."""
    fields = set(PolicyRules.model_fields)

    assert fields == {
        "requires_auth",
        "auth_criteria",
        "step_therapy_required",
        "step_therapy_details",
    }
    assert not fields & {"missing_criteria", "denial_risk", "nudge_message"}

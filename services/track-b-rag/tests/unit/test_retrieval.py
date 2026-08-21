"""What the vector search asks Qdrant for, and what it does with the answer.

The filter is the part worth pinning down. A policy ingested with no state
applies nationally — every CMS national coverage determination is one — and an
equality filter on ``state`` would hide all of them from a query that named a
state, which is a retrieval bug that looks exactly like "no policy indexed".
"""

from __future__ import annotations

from typing import Any

import pytest
from qdrant_client.models import FieldCondition, IsNullCondition

from track_b_rag import embeddings, retrieval


class FakePoint:
    def __init__(self, payload: dict[str, Any] | None, score: float = 0.5, id: str = "p1") -> None:
        self.payload = payload
        self.score = score
        self.id = id


class FakeResponse:
    def __init__(self, points: list[FakePoint]) -> None:
        self.points = points


class FakeQdrant:
    """Records the search it was asked to run and returns canned points."""

    def __init__(self, points: list[FakePoint] | None = None) -> None:
        self.points = points or []
        self.calls: list[dict[str, Any]] = []

    def query_points(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return FakeResponse(self.points)


@pytest.fixture
def stub_embedder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the 1.3 GB model with a deterministic vector, recording the text."""
    seen: list[str] = []

    def embed(text: str) -> list[float]:
        seen.append(text)
        return [0.1] * 8

    monkeypatch.setattr(embeddings, "embed_query", embed)
    return seen


def test_top_k_is_eight() -> None:
    """TASK-012 says top 8. Changing it changes the cost of every cache miss."""
    assert retrieval.TOP_K == 8


def test_the_filter_requires_the_payer() -> None:
    query_filter = retrieval.policy_query_filter(payer="Aetna", state="MA")

    assert query_filter.must is not None
    condition = list(query_filter.must)[0]
    assert isinstance(condition, FieldCondition)
    assert condition.key == "payer"
    assert condition.match is not None


def test_the_filter_admits_national_policies_alongside_the_state() -> None:
    """A policy with no state applies everywhere; an equality filter would hide it."""
    query_filter = retrieval.policy_query_filter(payer="Aetna", state="MA")

    assert query_filter.should is not None
    kinds = [type(condition) for condition in query_filter.should]
    assert FieldCondition in kinds
    assert IsNullCondition in kinds


def test_the_query_text_leads_with_the_code_and_still_names_the_procedure() -> None:
    """The code is what the answer is about; the words are what the policy text says."""
    text = retrieval.build_query_text(
        procedure="knee MRI",
        cpt_code="73721",
        payer="Aetna",
        plan_type="PPO",
    )

    assert text.index("73721") < text.index("knee MRI")
    assert "Aetna" in text
    assert "PPO" in text


def test_a_search_passes_the_vector_the_filter_and_the_limit(stub_embedder: list[str]) -> None:
    client = FakeQdrant([FakePoint({"text": "criteria", "policy_id": "L33575"})])

    retrieval.retrieve(
        client,
        collection="insurance_policies",
        procedure="knee MRI",
        cpt_code="73721",
        payer="Aetna",
        plan_type="PPO",
        state="MA",
    )

    call = client.calls[0]
    assert call["collection_name"] == "insurance_policies"
    assert call["limit"] == retrieval.TOP_K
    assert call["with_payload"] is True
    assert call["query"] == [0.1] * 8


def test_the_embedded_text_is_the_built_query(stub_embedder: list[str]) -> None:
    client = FakeQdrant()

    retrieval.retrieve(
        client,
        collection="insurance_policies",
        procedure="knee MRI",
        cpt_code="73721",
        payer="Aetna",
        plan_type="PPO",
        state="MA",
    )

    assert stub_embedder == [
        retrieval.build_query_text(
            procedure="knee MRI", cpt_code="73721", payer="Aetna", plan_type="PPO"
        )
    ]


def test_results_carry_the_text_the_policy_and_the_score(stub_embedder: list[str]) -> None:
    client = FakeQdrant(
        [
            FakePoint({"text": "first", "policy_id": "L33575"}, score=0.9),
            FakePoint({"text": "second", "policy_id": "L33575"}, score=0.7),
        ]
    )

    chunks = retrieval.retrieve(
        client,
        collection="insurance_policies",
        procedure="knee MRI",
        cpt_code="73721",
        payer="Aetna",
        plan_type="PPO",
        state="MA",
    )

    assert [chunk.text for chunk in chunks] == ["first", "second"]
    assert chunks[0].policy_id == "L33575"
    assert chunks[0].score == pytest.approx(0.9)


def test_a_point_with_no_text_is_skipped_and_logged(
    stub_embedder: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """An unusable point would only dilute the prompt, and says an ingest wrote
    a payload this service does not recognise."""
    client = FakeQdrant([FakePoint({"policy_id": "L33575"}), FakePoint({"text": "real"})])

    with caplog.at_level("WARNING"):
        chunks = retrieval.retrieve(
            client,
            collection="insurance_policies",
            procedure="knee MRI",
            cpt_code="73721",
            payer="Aetna",
            plan_type="PPO",
            state="MA",
        )

    assert [chunk.text for chunk in chunks] == ["real"]
    assert "carries no text" in caplog.text


def test_a_point_with_no_payload_at_all_is_skipped(stub_embedder: list[str]) -> None:
    client = FakeQdrant([FakePoint(None)])

    assert (
        retrieval.retrieve(
            client,
            collection="insurance_policies",
            procedure="knee MRI",
            cpt_code="73721",
            payer="Aetna",
            plan_type="PPO",
            state="MA",
        )
        == []
    )

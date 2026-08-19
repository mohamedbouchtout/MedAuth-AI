"""The embedder is lazy, cached, and never loaded at import time.

The real model is 1.3 GB and is not downloaded here — the actual (1024,)-shape
assertion is an integration test. What these check is the surrounding contract:
nothing loads until asked, the load happens once, and a failure to load is
reported rather than raised at a caller that cannot do anything about it.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

from track_b_rag import embeddings
from track_b_rag.embeddings import (
    check_health,
    embed_documents,
    embed_query,
    get_embedder,
    reset_embedder,
)


class FakeModel:
    """Stands in for SentenceTransformer, recording encode calls.

    ``encode`` takes either a string or a list, mirroring the real API: a single
    query returns one vector, a batch returns one per input.
    """

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector if vector is not None else [0.5] * 1024
        self.calls: list[tuple[str, bool]] = []
        self.batches: list[tuple[list[str], bool]] = []

    def encode(
        self, text: str | list[str], normalize_embeddings: bool = False
    ) -> list[float] | list[list[float]]:
        if isinstance(text, list):
            self.batches.append((text, normalize_embeddings))
            return [[float(index)] * 4 for index, _ in enumerate(text)]
        self.calls.append((text, normalize_embeddings))
        return self.vector


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    reset_embedder()
    yield
    reset_embedder()


@pytest.fixture
def loaded(monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    """Replace the loader so no weights are fetched."""
    model = FakeModel()
    monkeypatch.setattr(embeddings, "get_embedder", lambda: model)
    return model


def test_sentence_transformers_is_not_imported_at_module_scope() -> None:
    """It pulls in torch. Importing a route module must not pay seconds for that."""
    assert not hasattr(embeddings, "SentenceTransformer")


def test_the_model_loads_once_and_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[str] = []

    class FakeTransformerModule:
        @staticmethod
        def SentenceTransformer(name: str) -> FakeModel:
            loads.append(name)
            return FakeModel()

    monkeypatch.setitem(sys.modules, "sentence_transformers", FakeTransformerModule)

    first = get_embedder()
    second = get_embedder()

    assert first is second
    assert loads == ["BAAI/bge-large-en-v1.5"]


def test_embed_query_normalises(loaded: FakeModel) -> None:
    """The collection is cosine, so vectors go in normalised."""
    embed_query("does an MRI of the knee need prior auth")

    assert loaded.calls == [("does an MRI of the knee need prior auth", True)]


def test_embed_query_returns_plain_floats(loaded: FakeModel) -> None:
    """Qdrant takes a list of floats, not a numpy array."""
    vector = embed_query("knee MRI")

    assert isinstance(vector, list)
    assert all(type(value) is float for value in vector)
    assert len(vector) == 1024


def test_health_is_true_when_the_model_loads(loaded: FakeModel) -> None:
    assert check_health() is True


def test_health_is_false_when_the_model_cannot_load(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode() -> FakeModel:
        raise OSError("no space left on device")

    monkeypatch.setattr(embeddings, "get_embedder", explode)

    assert check_health() is False


def test_embedding_documents_returns_one_vector_per_chunk(loaded: FakeModel) -> None:
    vectors = embed_documents(["first chunk", "second chunk", "third chunk"])

    assert len(vectors) == 3


def test_chunks_keep_their_order(loaded: FakeModel) -> None:
    vectors = embed_documents(["a", "b"])

    assert vectors == [[0.0] * 4, [1.0] * 4]


def test_a_batch_is_one_encode_call_not_a_loop(loaded: FakeModel) -> None:
    """The model parallelises a batch internally; a policy PDF is dozens of chunks."""
    embed_documents(["a", "b", "c"])

    assert len(loaded.batches) == 1
    assert loaded.batches[0][0] == ["a", "b", "c"]


def test_documents_are_normalised_like_queries(loaded: FakeModel) -> None:
    """The collection compares with cosine distance — both sides must be unit length."""
    embed_documents(["a"])

    assert loaded.batches[0][1] is True


def test_embedding_nothing_loads_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty chunk list must not pay for a 1.3 GB load to return []."""

    def explode() -> FakeModel:
        raise AssertionError("the model must not be loaded for an empty batch")

    monkeypatch.setattr(embeddings, "get_embedder", explode)

    assert embed_documents([]) == []


def test_values_are_plain_floats(loaded: FakeModel) -> None:
    """numpy scalars would not serialise into a Qdrant payload."""
    vectors = embed_documents(["a"])

    assert all(type(value) is float for value in vectors[0])

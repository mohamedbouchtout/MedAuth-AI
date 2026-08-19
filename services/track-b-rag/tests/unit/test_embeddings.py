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
from track_b_rag.embeddings import check_health, embed_query, get_embedder, reset_embedder


class FakeModel:
    """Stands in for SentenceTransformer, recording encode calls."""

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector if vector is not None else [0.5] * 1024
        self.calls: list[tuple[str, bool]] = []

    def encode(self, text: str, normalize_embeddings: bool = False) -> list[float]:
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

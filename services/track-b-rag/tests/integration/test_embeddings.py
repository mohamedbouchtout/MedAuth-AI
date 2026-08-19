"""The real embedding model — the one place the (1024,) shape is actually proven.

Loading ``BAAI/bge-large-en-v1.5`` downloads roughly 1.3 GB the first time, so
this is an integration test rather than part of the unit suite, and CI caches
``~/.cache/huggingface`` so the download happens once rather than every run.

Skipped unless RUN_EMBEDDING_TESTS is set, which the CI test job sets. A
developer running ``pytest tests/unit`` on a laptop should not silently start a
gigabyte download; a developer who wants this can opt in.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from track_b_rag.config import DEFAULT_EMBEDDING_DIMENSIONS, get_settings
from track_b_rag.embeddings import check_health, embed_query, get_embedder, reset_embedder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("RUN_EMBEDDING_TESTS"),
        reason="RUN_EMBEDDING_TESTS is not set — this downloads ~1.3GB of weights",
    ),
]


@pytest.fixture(scope="module", autouse=True)
def loaded_once() -> Iterator[None]:
    """Load the model once for the module, then release it."""
    get_settings.cache_clear()
    reset_embedder()
    yield
    reset_embedder()
    get_settings.cache_clear()


def test_the_model_produces_a_1024_dimensional_vector() -> None:
    """TASK-010's shape assertion. 1024 is bge-large's width and the collection's."""
    vector = embed_query("does an MRI of the knee require prior authorization")

    assert len(vector) == DEFAULT_EMBEDDING_DIMENSIONS == 1024
    assert all(isinstance(value, float) for value in vector)


def test_the_raw_encoder_agrees_on_the_shape() -> None:
    """Guards against embed_query padding or truncating on its way out."""
    raw = get_embedder().encode("knee MRI", normalize_embeddings=True)

    assert raw.shape == (1024,)


def test_vectors_are_normalised_for_cosine() -> None:
    """The collection is cosine; unnormalised vectors would skew every ranking."""
    vector = embed_query("total knee arthroplasty medical necessity criteria")
    magnitude = sum(value * value for value in vector) ** 0.5

    assert magnitude == pytest.approx(1.0, abs=1e-4)


def test_related_text_scores_above_unrelated_text() -> None:
    """A sanity check that the weights loaded are the ones we think they are."""

    def cosine(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    query = embed_query("knee MRI prior authorization criteria")
    related = embed_query("magnetic resonance imaging of the knee joint requires preauthorization")
    unrelated = embed_query("annual influenza vaccination schedule for adults")

    assert cosine(query, related) > cosine(query, unrelated)


def test_health_is_true_once_the_real_model_loads() -> None:
    assert check_health() is True

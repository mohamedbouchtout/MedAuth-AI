"""The local sentence-transformers embedder.

``BAAI/bge-large-en-v1.5`` is roughly 1.3 GB of weights and takes seconds to
load, so it is a lazily-created singleton rather than a module-level object: the
service must start without paying for the load, and a worker that never embeds
anything should never pay for it at all. The first call to :func:`get_embedder`
loads it; every call after that reuses it.

Local, not an API. Policy text is not PHI, but keeping embedding on-cluster
means the RAG path has no external dependency to fail, rate-limit, or leak to.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from functools import lru_cache
from typing import TYPE_CHECKING

from track_b_rag.config import get_settings

if TYPE_CHECKING:  # pragma: no cover - import cost is the reason for the guard
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_embedder() -> SentenceTransformer:
    """Return the process-wide embedding model, loading it on first use.

    The ``sentence_transformers`` import happens here rather than at module
    scope: it pulls in torch, which costs seconds and hundreds of megabytes of
    resident memory, and nothing should pay that just to import a route module.
    """
    from sentence_transformers import SentenceTransformer

    settings = get_settings()
    logger.info("Loading embedding model %r", settings.embedding_model_name)
    # Annotated because the package's constructor is untyped under mypy strict;
    # without it the return is Any and the declared type here means nothing.
    model: SentenceTransformer = SentenceTransformer(settings.embedding_model_name)
    return model


def reset_embedder() -> None:
    """Forget the loaded model. For tests and shutdown, not for request paths."""
    get_embedder.cache_clear()


def embed_query(text: str) -> list[float]:
    """Return the embedding for a single query string.

    The vector is ``EMBEDDING_DIMENSIONS`` long (1024 for bge-large) and
    normalised, so a cosine search in Qdrant compares unit vectors — the
    distance the collection is configured with in
    :mod:`track_b_rag.vector_store`.
    """
    embedder = get_embedder()
    vector = embedder.encode(text, normalize_embeddings=True)
    return [float(value) for value in vector]


def embed_documents(texts: Sequence[str]) -> list[list[float]]:
    """Return one embedding per input text, in the same order.

    Batched through a single ``encode`` call rather than looped: the model
    parallelises a batch internally, and a policy PDF chunks into dozens or
    hundreds of texts. Normalised for the same reason :func:`embed_query` is —
    the collection compares with cosine distance.
    """
    if not texts:
        return []
    embedder = get_embedder()
    vectors = embedder.encode(list(texts), normalize_embeddings=True)
    return [[float(value) for value in vector] for vector in vectors]


def check_health() -> bool:
    """Return whether the embedding model is loadable. Used by ``GET /health``.

    This forces the load if it has not happened yet, so the first probe after
    startup is the one that pays for it and the endpoint reports "error" until
    the weights are actually in memory. That is the intended reading: a replica
    that cannot embed is not ready to serve queries.
    """
    try:
        get_embedder()
    except Exception:  # a missing cache, an OOM, or a bad model name are all "not ok"
        logger.warning("Embedding model health check failed", exc_info=True)
        return False
    return True

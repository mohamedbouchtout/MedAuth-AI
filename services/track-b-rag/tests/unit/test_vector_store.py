"""ensure_collection creates only what is missing — the recreate_collection guard.

These are the fast checks against a fake client. The claim that actually matters
— that a second call preserves the points in a populated collection — cannot be
proven against a fake, and lives in ``tests/integration/test_vector_store.py``
against a real container.
"""

from __future__ import annotations

from typing import Any

import pytest
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Distance

from track_b_rag import vector_store
from track_b_rag.vector_store import check_health, close_client, ensure_collection, get_client

COLLECTION = "insurance_policies"
VECTOR_SIZE = 1024


def not_found() -> UnexpectedResponse:
    """The error a Qdrant HTTP client raises for a collection that is absent."""
    return UnexpectedResponse(status_code=404, reason_phrase="Not Found", content=b"", headers=None)


def conflict() -> UnexpectedResponse:
    """The error raised when another replica created the collection first."""
    return UnexpectedResponse(status_code=409, reason_phrase="Conflict", content=b"", headers=None)


class FakeClient:
    """Just enough QdrantClient for the get-or-create path.

    ``recreate_collection`` is deliberately absent rather than recorded: if
    production code ever calls it, these tests fail with AttributeError instead
    of quietly passing.
    """

    def __init__(
        self,
        *,
        existing: set[str] | None = None,
        create_error: Exception | None = None,
        get_error: Exception | None = None,
    ) -> None:
        self.existing = existing if existing is not None else set()
        self.create_error = create_error
        self.get_error = get_error
        self.created: list[tuple[str, int, Distance]] = []
        self.get_calls = 0
        self.closed = False

    def get_collection(self, name: str) -> object:
        self.get_calls += 1
        if self.get_error is not None:
            raise self.get_error
        if name not in self.existing:
            raise not_found()
        return object()

    def create_collection(self, collection_name: str, vectors_config: Any) -> None:
        if self.create_error is not None:
            raise self.create_error
        self.created.append((collection_name, vectors_config.size, vectors_config.distance))
        self.existing.add(collection_name)

    def get_collections(self) -> object:
        if self.get_error is not None:
            raise self.get_error
        return object()

    def close(self) -> None:
        self.closed = True


def test_creates_the_collection_when_it_is_absent() -> None:
    client = FakeClient()

    created = ensure_collection(client, COLLECTION, VECTOR_SIZE)  # type: ignore[arg-type]

    assert created is True
    assert client.created == [(COLLECTION, VECTOR_SIZE, Distance.COSINE)]


def test_leaves_an_existing_collection_alone() -> None:
    client = FakeClient(existing={COLLECTION})

    created = ensure_collection(client, COLLECTION, VECTOR_SIZE)  # type: ignore[arg-type]

    assert created is False
    assert client.created == []


def test_calling_twice_creates_once() -> None:
    """Startup runs this on every boot; the second boot must be a no-op."""
    client = FakeClient()

    first = ensure_collection(client, COLLECTION, VECTOR_SIZE)  # type: ignore[arg-type]
    second = ensure_collection(client, COLLECTION, VECTOR_SIZE)  # type: ignore[arg-type]

    assert (first, second) == (True, False)
    assert len(client.created) == 1


def test_a_concurrent_creation_is_success_not_failure() -> None:
    """Several replicas boot together; losing the race still means it exists."""
    client = FakeClient(create_error=conflict())
    # The collection appears between our get_collection and our create_collection.
    client.existing.add(COLLECTION)
    original_get = client.get_collection
    calls = {"n": 0}

    def get_collection(name: str) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise not_found()
        return original_get(name)

    client.get_collection = get_collection  # type: ignore[method-assign]

    assert ensure_collection(client, COLLECTION, VECTOR_SIZE) is False  # type: ignore[arg-type]


def test_a_real_create_failure_propagates() -> None:
    """A 409 we cannot explain by the collection existing is still an error."""
    client = FakeClient(create_error=conflict())

    with pytest.raises(UnexpectedResponse):
        ensure_collection(client, COLLECTION, VECTOR_SIZE)  # type: ignore[arg-type]


def test_ensure_collection_never_recreates() -> None:
    """The regression guard, stated directly: no recreate_collection anywhere."""
    source = (vector_store.__file__ or "").replace("\\", "/")
    assert source.endswith("vector_store.py")
    with open(source, encoding="utf-8") as handle:
        body = handle.read()

    # The module docstring names the banned function to explain the ban, so
    # match an actual call site — a method call on a client — not the mention.
    assert ".recreate_collection(" not in body


def test_health_is_true_when_qdrant_answers() -> None:
    assert check_health(FakeClient()) is True  # type: ignore[arg-type]


def test_health_is_false_when_qdrant_raises() -> None:
    client = FakeClient(get_error=ConnectionError("qdrant is down"))

    assert check_health(client) is False  # type: ignore[arg-type]


def test_the_client_is_a_singleton_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[FakeClient] = []

    def fake_qdrant_client(**_kwargs: Any) -> FakeClient:
        client = FakeClient()
        made.append(client)
        return client

    monkeypatch.setattr(vector_store, "QdrantClient", fake_qdrant_client)
    close_client()

    assert get_client() is get_client()
    assert len(made) == 1

    close_client()

    assert made[0].closed is True
    assert get_client.cache_info().currsize == 0
    close_client()


def test_closing_an_unopened_client_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(**_kwargs: Any) -> FakeClient:
        raise AssertionError("close_client must not construct a client")

    close_client()
    monkeypatch.setattr(vector_store, "QdrantClient", explode)

    close_client()

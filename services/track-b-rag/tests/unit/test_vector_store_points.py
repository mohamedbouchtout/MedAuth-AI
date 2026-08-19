"""Payload indexes, deterministic point IDs, and the delete-by-policy path.

Separate from ``test_vector_store.py``, which is entirely about the
recreate_collection guard, so that regression suite stays readable as the one
thing it is.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    FieldCondition,
    FilterSelector,
    PayloadSchemaType,
    PointStruct,
)

from track_b_rag.vector_store import (
    INDEXED_PAYLOAD_FIELDS,
    PAYLOAD_FIELDS,
    POINT_NAMESPACE,
    build_points,
    count_policy_points,
    delete_policy_points,
    ensure_payload_indexes,
    point_id,
    policy_filter,
    upsert_points,
)

COLLECTION = "insurance_policies"


def not_found() -> UnexpectedResponse:
    return UnexpectedResponse(status_code=404, reason_phrase="Not Found", content=b"", headers=None)


class FakeCollection:
    def __init__(self, payload_schema: dict[str, object] | None) -> None:
        self.payload_schema = payload_schema


class FakeCount:
    def __init__(self, count: int) -> None:
        self.count = count


class FakeClient:
    """Just enough QdrantClient for the ingestion write paths."""

    def __init__(
        self,
        *,
        indexed: dict[str, object] | None = None,
        missing_collection: bool = False,
        count: int = 0,
    ) -> None:
        self.indexed = indexed
        self.missing_collection = missing_collection
        self.count_result = count
        self.created_indexes: list[tuple[str, PayloadSchemaType]] = []
        self.upserted: list[list[PointStruct]] = []
        self.deleted: list[tuple[str, Any]] = []
        self.counted: list[Any] = []

    def get_collection(self, name: str) -> FakeCollection:
        if self.missing_collection:
            raise not_found()
        return FakeCollection(self.indexed)

    def create_payload_index(
        self, *, collection_name: str, field_name: str, field_schema: PayloadSchemaType
    ) -> None:
        self.created_indexes.append((field_name, field_schema))

    def upsert(self, *, collection_name: str, points: list[PointStruct], wait: bool) -> None:
        assert wait is True
        self.upserted.append(points)

    def delete(self, *, collection_name: str, points_selector: Any, wait: bool) -> None:
        assert wait is True
        self.deleted.append((collection_name, points_selector))

    def count(self, *, collection_name: str, count_filter: Any, exact: bool) -> FakeCount:
        assert exact is True
        self.counted.append(count_filter)
        return FakeCount(self.count_result)


# --- payload indexes -------------------------------------------------------


def test_missing_indexes_are_created() -> None:
    client = FakeClient(indexed={})

    created = ensure_payload_indexes(client, COLLECTION)  # type: ignore[arg-type]

    assert created == INDEXED_PAYLOAD_FIELDS
    assert [field for field, _ in client.created_indexes] == list(INDEXED_PAYLOAD_FIELDS)


def test_indexes_are_keyword_typed() -> None:
    client = FakeClient(indexed={})

    ensure_payload_indexes(client, COLLECTION)  # type: ignore[arg-type]

    assert {schema for _, schema in client.created_indexes} == {PayloadSchemaType.KEYWORD}


def test_existing_indexes_are_left_alone() -> None:
    """create_payload_index rebuilds over the whole collection — never unconditionally."""
    client = FakeClient(indexed=dict.fromkeys(INDEXED_PAYLOAD_FIELDS, object()))

    created = ensure_payload_indexes(client, COLLECTION)  # type: ignore[arg-type]

    assert created == ()
    assert client.created_indexes == []


def test_only_the_absent_index_is_created() -> None:
    client = FakeClient(indexed={"policy_id": object()})

    created = ensure_payload_indexes(client, COLLECTION)  # type: ignore[arg-type]

    assert "policy_id" not in created
    assert set(created) == set(INDEXED_PAYLOAD_FIELDS) - {"policy_id"}


def test_a_second_call_creates_nothing_new() -> None:
    client = FakeClient(indexed={})

    ensure_payload_indexes(client, COLLECTION)  # type: ignore[arg-type]
    client.indexed = dict.fromkeys(INDEXED_PAYLOAD_FIELDS, object())
    before = len(client.created_indexes)

    assert ensure_payload_indexes(client, COLLECTION) == ()  # type: ignore[arg-type]
    assert len(client.created_indexes) == before


def test_an_unreachable_collection_is_treated_as_unindexed() -> None:
    """Startup logs and carries on; /health is what reports the failure."""
    client = FakeClient(missing_collection=True)

    ensure_payload_indexes(client, COLLECTION)  # type: ignore[arg-type]

    assert [field for field, _ in client.created_indexes] == list(INDEXED_PAYLOAD_FIELDS)


def test_a_null_payload_schema_is_treated_as_unindexed() -> None:
    client = FakeClient(indexed=None)

    assert ensure_payload_indexes(client, COLLECTION) == INDEXED_PAYLOAD_FIELDS  # type: ignore[arg-type]


def test_the_indexed_fields_serve_the_two_known_query_paths() -> None:
    assert set(INDEXED_PAYLOAD_FIELDS) == {"policy_id", "payer", "state"}


# --- point identity --------------------------------------------------------


def test_point_ids_are_deterministic() -> None:
    assert point_id("L33575", 4) == point_id("L33575", 4)


def test_point_ids_differ_per_chunk() -> None:
    assert point_id("L33575", 0) != point_id("L33575", 1)


def test_point_ids_differ_per_policy() -> None:
    assert point_id("L33575", 0) != point_id("L33576", 0)


def test_point_ids_come_from_the_fixed_namespace() -> None:
    """Regenerating the namespace would orphan every point already indexed."""
    assert point_id("L33575", 7) == str(uuid.uuid5(POINT_NAMESPACE, "L33575:7"))


def test_the_namespace_is_pinned() -> None:
    assert POINT_NAMESPACE == uuid.UUID("9d0f6a2b-1c84-4f5a-b3d7-0e6a5c1f8b42")


# --- building points -------------------------------------------------------


def test_points_carry_the_whole_payload_schema() -> None:
    points = build_points(
        policy_id="L33575",
        payer="CMS",
        plan_type="Medicare",
        state="NY",
        chunks=["criteria text"],
        vectors=[[0.5, 0.5]],
    )

    assert set(points[0].payload or {}) == set(PAYLOAD_FIELDS)


def test_payload_values_are_what_was_passed() -> None:
    points = build_points(
        policy_id="L33575",
        payer="Aetna",
        plan_type="PPO",
        state="NY",
        chunks=["first", "second"],
        vectors=[[1.0], [2.0]],
    )

    assert points[1].payload == {
        "policy_id": "L33575",
        "payer": "Aetna",
        "plan_type": "PPO",
        "state": "NY",
        "chunk_index": 1,
        "text": "second",
    }


def test_a_national_policy_carries_null_state_and_plan_type() -> None:
    points = build_points(
        policy_id="NCD-220.1",
        payer="CMS",
        plan_type=None,
        state=None,
        chunks=["nationwide"],
        vectors=[[1.0]],
    )

    payload = points[0].payload or {}
    assert payload["state"] is None
    assert payload["plan_type"] is None


def test_chunks_keep_their_order_and_their_own_vectors() -> None:
    points = build_points(
        policy_id="L1",
        payer="CMS",
        plan_type=None,
        state=None,
        chunks=["a", "b", "c"],
        vectors=[[1.0], [2.0], [3.0]],
    )

    assert [point.vector for point in points] == [[1.0], [2.0], [3.0]]
    assert [(point.payload or {})["text"] for point in points] == ["a", "b", "c"]


def test_points_use_the_deterministic_ids() -> None:
    points = build_points(
        policy_id="L33575",
        payer="CMS",
        plan_type=None,
        state=None,
        chunks=["a", "b"],
        vectors=[[1.0], [2.0]],
    )

    assert [point.id for point in points] == [point_id("L33575", 0), point_id("L33575", 1)]


def test_mismatched_counts_are_refused() -> None:
    """Pairing by position is only safe when the two sequences agree in length."""
    with pytest.raises(ValueError, match="refusing to pair"):
        build_points(
            policy_id="L1",
            payer="CMS",
            plan_type=None,
            state=None,
            chunks=["a", "b"],
            vectors=[[1.0]],
        )


def test_no_chunks_builds_no_points() -> None:
    assert (
        build_points(policy_id="L1", payer="CMS", plan_type=None, state=None, chunks=[], vectors=[])
        == []
    )


# --- writing and removing --------------------------------------------------


def test_deleting_selects_by_policy_id_not_by_reconstructed_ids() -> None:
    """A shorter revision must not strand the previous version's tail."""
    client = FakeClient()

    delete_policy_points(client, COLLECTION, "L33575")  # type: ignore[arg-type]

    _, selector = client.deleted[0]
    assert isinstance(selector, FilterSelector)
    condition = selector.filter.must[0]  # type: ignore[union-attr,index]
    assert isinstance(condition, FieldCondition)
    assert condition.key == "policy_id"
    assert condition.match.value == "L33575"  # type: ignore[union-attr]


def test_upserting_writes_every_point() -> None:
    client = FakeClient()
    points = build_points(
        policy_id="L1", payer="CMS", plan_type=None, state=None, chunks=["a"], vectors=[[1.0]]
    )

    upsert_points(client, COLLECTION, points)  # type: ignore[arg-type]

    assert client.upserted == [points]


def test_upserting_nothing_touches_qdrant_not_at_all() -> None:
    client = FakeClient()

    upsert_points(client, COLLECTION, [])  # type: ignore[arg-type]

    assert client.upserted == []


def test_counting_filters_to_the_one_policy() -> None:
    client = FakeClient(count=12)

    assert count_policy_points(client, COLLECTION, "L33575") == 12  # type: ignore[arg-type]
    assert client.counted == [policy_filter("L33575")]

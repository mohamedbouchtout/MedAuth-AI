"""ensure_collection against a real Qdrant — the recreate_collection regression test.

This is the one TASK-010 exists to prevent. ``recreate_collection()`` drops the
collection and builds an empty one, so a service that calls it on startup wipes
every indexed policy on each restart. The claim that a second
``ensure_collection`` preserves the data cannot be proven against a mock — a
fake will happily report whatever it was told to — so it runs against the
container that docker-compose and the CI ``test`` job both provide.

Skipped when QDRANT_HOST is unset, so the unit suite still runs on a machine
with nothing up. Each test uses its own collection name and removes it
afterwards, so a shared CI container is not left dirty for the next member.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct

from track_b_rag.vector_store import DISTANCE, check_health, ensure_collection

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("QDRANT_HOST"),
        reason="QDRANT_HOST is not set — these tests need a real Qdrant",
    ),
]

VECTOR_SIZE = 1024


@pytest.fixture
def client() -> Iterator[QdrantClient]:
    connection = QdrantClient(
        host=os.environ["QDRANT_HOST"],
        port=int(os.environ.get("QDRANT_PORT", "6333")),
        api_key=os.environ.get("QDRANT_API_KEY") or None,
    )
    yield connection
    connection.close()


@pytest.fixture
def collection(client: QdrantClient) -> Iterator[str]:
    """A collection name unique to this test, deleted afterwards."""
    name = f"test_policies_{uuid.uuid4().hex}"
    yield name
    client.delete_collection(name)


def points(count: int) -> list[PointStruct]:
    """Stand-in policy chunks. Distinct vectors so a wipe is unmistakable."""
    return [
        PointStruct(
            id=index + 1,
            vector=[float(index + 1) / VECTOR_SIZE] * VECTOR_SIZE,
            payload={"policy_id": f"L{index + 1:05d}", "payer": "CMS"},
        )
        for index in range(count)
    ]


def test_the_collection_is_created_with_the_specified_geometry(
    client: QdrantClient, collection: str
) -> None:
    assert ensure_collection(client, collection, VECTOR_SIZE) is True

    info = client.get_collection(collection)
    params = info.config.params.vectors
    assert params is not None
    assert params.size == VECTOR_SIZE
    assert params.distance == Distance.COSINE == DISTANCE


def test_a_second_call_preserves_every_indexed_policy(
    client: QdrantClient, collection: str
) -> None:
    """The regression test. recreate_collection() here would return 0 points."""
    ensure_collection(client, collection, VECTOR_SIZE)
    client.upsert(collection_name=collection, points=points(5), wait=True)
    assert client.count(collection, exact=True).count == 5

    created_again = ensure_collection(client, collection, VECTOR_SIZE)

    assert created_again is False
    assert client.count(collection, exact=True).count == 5

    stored = client.retrieve(collection_name=collection, ids=[1, 2, 3, 4, 5])
    assert {record.payload["policy_id"] for record in stored if record.payload} == {
        "L00001",
        "L00002",
        "L00003",
        "L00004",
        "L00005",
    }


def test_restarting_repeatedly_never_drops_data(client: QdrantClient, collection: str) -> None:
    """A pod that restart-loops must not erode the index one restart at a time."""
    ensure_collection(client, collection, VECTOR_SIZE)
    client.upsert(collection_name=collection, points=points(3), wait=True)

    for _ in range(5):
        ensure_collection(client, collection, VECTOR_SIZE)

    assert client.count(collection, exact=True).count == 3


def test_health_reports_a_reachable_qdrant(client: QdrantClient) -> None:
    assert check_health(client) is True


def test_health_reports_an_unreachable_qdrant() -> None:
    unreachable = QdrantClient(host="127.0.0.1", port=1, timeout=1)

    assert check_health(unreachable) is False

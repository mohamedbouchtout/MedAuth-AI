"""``GET /health`` — the envelope, the flags, and the 503 that still carries data."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from src.api.dependencies import get_redis
from src.main import create_app

from .conftest import FakeRedis


@pytest.fixture
def unhealthy_client(smart_env: None) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: FakeRedis(healthy=False)
    with TestClient(app) as test_client:
        yield test_client


def test_healthy_returns_200_in_the_envelope(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"data": {"redis": "ok"}, "error": None}


def test_unreachable_redis_returns_503(unhealthy_client: TestClient) -> None:
    response = unhealthy_client.get("/health")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_a_503_still_carries_data_not_error(unhealthy_client: TestClient) -> None:
    """The request succeeded; the answer is 'unhealthy'. The flags are the diagnostic."""
    body = unhealthy_client.get("/health").json()

    assert body["error"] is None
    assert body["data"] == {"redis": "error"}

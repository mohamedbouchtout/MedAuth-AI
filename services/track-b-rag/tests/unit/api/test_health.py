"""GET /health — envelope shape, status codes, and the audit exemption."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from httpx import AsyncClient

from tests.unit.api.conftest import FakeQdrant


async def test_both_up_is_200_in_the_standard_envelope(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "data": {"qdrant": "ok", "embedding_model": "ok"},
        "error": None,
    }


async def test_qdrant_down_is_503_and_names_qdrant(
    client: AsyncClient, fake_qdrant: FakeQdrant
) -> None:
    fake_qdrant.healthy = False

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["data"] == {"qdrant": "error", "embedding_model": "ok"}


async def test_the_model_down_is_503_and_names_the_model(
    client: AsyncClient, embedding_health: Callable[[bool], None]
) -> None:
    embedding_health(False)

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["data"] == {"qdrant": "ok", "embedding_model": "error"}


async def test_both_down_is_503(
    client: AsyncClient, fake_qdrant: FakeQdrant, embedding_health: Callable[[bool], None]
) -> None:
    fake_qdrant.healthy = False
    embedding_health(False)

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["data"] == {"qdrant": "error", "embedding_model": "error"}


@pytest.mark.parametrize("qdrant_up", [True, False])
async def test_a_503_still_carries_data_not_an_error(
    client: AsyncClient, fake_qdrant: FakeQdrant, qdrant_up: bool
) -> None:
    """The request succeeded; the answer is "unhealthy". The flags are the point."""
    fake_qdrant.healthy = qdrant_up

    body = (await client.get("/health")).json()

    assert body["error"] is None
    assert set(body["data"]) == {"qdrant", "embedding_model"}


async def test_health_writes_no_audit_row(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The standing exemption in CLAUDE.md, asserted rather than assumed.

    A probe runs on a fixed interval forever; auditing it would bury real PHI
    access in noise and contradicts hipaa-logger's own scope note.
    """
    import hipaa_logger

    calls: list[object] = []

    async def record(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(hipaa_logger, "audit_log", record)

    await client.get("/health")

    assert calls == []


async def test_an_unknown_route_still_gets_the_envelope(client: AsyncClient) -> None:
    """The error handlers are installed, so a 404 is not FastAPI's {"detail": ...}."""
    response = await client.get("/no-such-route")

    assert response.status_code == 404
    body = response.json()
    assert body["data"] is None
    assert body["error"]["code"] == "http_error"

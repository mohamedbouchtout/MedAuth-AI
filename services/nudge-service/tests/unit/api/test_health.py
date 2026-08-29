"""``GET /health`` — the envelope on both sides of the 200/503 line."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.dependencies import get_redis
from src.main import create_app
from tests.unit.api.conftest import FakeRedis


def client_with(redis: FakeRedis) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_redis] = lambda: redis
    return TestClient(app)


def test_a_reachable_bus_reports_healthy(signing_key: str) -> None:
    response = client_with(FakeRedis()).get("/health")

    assert response.status_code == 200
    assert response.json() == {"data": {"redis": "ok"}, "error": None}


def test_an_unreachable_bus_is_a_503_that_still_names_the_dependency(
    signing_key: str,
) -> None:
    """A nudge this service cannot receive is a nudge the provider never sees."""
    response = client_with(FakeRedis(healthy=False)).get("/health")

    assert response.status_code == 503
    assert response.json()["data"] == {"redis": "error"}


def test_the_503_carries_data_rather_than_an_error(signing_key: str) -> None:
    """The documented departure from the envelope's failure half.

    The request succeeded and the answer is "unhealthy"; moving the flags into
    the error half would discard the only diagnostic the endpoint has.
    """
    body = client_with(FakeRedis(healthy=False)).get("/health").json()

    assert body["error"] is None
    assert body["data"] is not None


def test_the_probe_writes_no_audit_row(signing_key: str) -> None:
    """Known Constraints #6: no PHI, no row.

    Auditing a Kubernetes probe on its polling interval is exactly the dilution
    that rule exists to prevent — it would make "who accessed patient X" a query
    you have to filter rather than one you can just run.
    """
    from src.api import health as health_module

    assert not hasattr(health_module, "audit_nudge_stream")

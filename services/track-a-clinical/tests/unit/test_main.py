"""The application factory and its shutdown lifespan."""

from __future__ import annotations

import pytest

from track_a_clinical import db, main
from track_a_clinical.api import dependencies
from track_a_clinical.main import create_app


def test_app_exposes_both_session_routes() -> None:
    paths = create_app().openapi()["paths"]

    assert set(paths) == {"/sessions/start", "/sessions/{session_id}/end"}
    assert set(paths["/sessions/start"]) == {"post"}


def test_each_app_is_independent() -> None:
    """The factory exists so a test can override dependencies without leaking."""
    first, second = create_app(), create_app()
    first.dependency_overrides[dependencies.get_redis] = lambda: None

    assert second.dependency_overrides == {}


def test_start_documents_the_created_status() -> None:
    """201, not 200 — the call creates an encounter. Clients read this from the spec."""
    operation = create_app().openapi()["paths"]["/sessions/start"]["post"]

    assert set(operation["responses"]) >= {"201"}


async def test_lifespan_releases_both_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing is opened on startup; both pools are released on the way down."""
    released: list[str] = []

    async def record_engine() -> None:
        released.append("engine")

    async def record_redis() -> None:
        released.append("redis")

    monkeypatch.setattr(main, "dispose_engine", record_engine)
    monkeypatch.setattr(main, "close_redis", record_redis)

    app = create_app()
    async with main.lifespan(app):
        assert released == []

    assert released == ["engine", "redis"]


def test_lifespan_uses_the_real_release_hooks() -> None:
    """Guards the monkeypatching above against renamed hooks."""
    assert main.dispose_engine is db.dispose_engine
    assert main.close_redis is dependencies.close_redis

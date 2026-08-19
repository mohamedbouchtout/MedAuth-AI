"""The envelope handlers, including the paths no route reaches yet.

``/health`` takes no request body, so the validation handler has nothing to
reject until TASK-012 adds ``/policies/query``. It is tested here against a
throwaway route rather than left uncovered — the reason it exists is that a
future body carries a patient's clinical context, and the rule that only field
*locations* are echoed has to hold from the first route that has a body, not be
discovered then.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from track_b_rag.api.dependencies import get_qdrant
from track_b_rag.api.envelope import ApiResponse, install_error_handlers
from track_b_rag.vector_store import close_client


class Body(BaseModel):
    cpt_code: str
    clinical_context: dict[str, str]


def build_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/echo", response_model=ApiResponse[Body])
    async def echo(body: Body) -> ApiResponse[Body]:
        return ApiResponse[Body](data=body)

    return app


async def post(payload: Any) -> tuple[int, dict[str, Any]]:
    transport = ASGITransport(app=build_app())
    async with AsyncClient(transport=transport, base_url="http://track-b-rag") as http:
        response = await http.post("/echo", json=payload)
    return response.status_code, response.json()


async def test_a_valid_body_round_trips_in_the_envelope() -> None:
    status_code, body = await post({"cpt_code": "72148", "clinical_context": {"side": "left"}})

    assert status_code == 200
    assert body == {
        "data": {"cpt_code": "72148", "clinical_context": {"side": "left"}},
        "error": None,
    }


async def test_a_missing_field_is_a_422_in_the_envelope() -> None:
    status_code, body = await post({"clinical_context": {}})

    assert status_code == 422
    assert body["data"] is None
    assert body["error"]["code"] == "validation_error"
    assert "cpt_code" in body["error"]["message"]


async def test_the_rejected_value_is_never_echoed_back() -> None:
    """A request body here will carry clinical context. Locations only, no values."""
    status_code, body = await post(
        {"cpt_code": "72148", "clinical_context": "PATIENT-SPECIFIC-SECRET"}
    )

    assert status_code == 422
    message = body["error"]["message"]
    assert "clinical_context" in message
    assert "PATIENT-SPECIFIC-SECRET" not in message


async def test_the_qdrant_dependency_returns_the_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real dependency body — every other test replaces it with a fake."""
    sentinel = object()
    monkeypatch.setattr("track_b_rag.api.dependencies.get_client", lambda: sentinel)

    assert await get_qdrant() is sentinel  # type: ignore[comparison-overlap]

    close_client()

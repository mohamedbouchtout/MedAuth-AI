"""The error handlers, the OpenAPI declarations, and the no-values-echoed rule."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from api_envelope import (
    ERROR_CODE_HTTP,
    ERROR_CODE_VALIDATION,
    ApiHTTPException,
    ApiResponse,
    error_response,
    error_responses,
    install_error_handlers,
)


class Body(BaseModel):
    cpt_code: str
    clinical_context: dict[str, str]


ERROR_CODE_CUSTOM = "session_not_found"


def build_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/echo", response_model=ApiResponse[Body])
    async def echo(body: Body) -> ApiResponse[Body]:
        return ApiResponse[Body](data=body)

    @app.get("/coded")
    async def coded() -> None:
        raise ApiHTTPException(status.HTTP_404_NOT_FOUND, ERROR_CODE_CUSTOM, "No such session")

    @app.get("/bare")
    async def bare() -> None:
        from fastapi import HTTPException

        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Nope")

    return app


async def call(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    transport = ASGITransport(app=build_app())
    async with AsyncClient(transport=transport, base_url="http://svc") as http:
        response = await http.request(method, path, **kwargs)
    return response.status_code, response.json()


async def test_a_valid_body_round_trips() -> None:
    status_code, body = await call(
        "POST", "/echo", json={"cpt_code": "72148", "clinical_context": {"side": "left"}}
    )

    assert status_code == 200
    assert body == {
        "data": {"cpt_code": "72148", "clinical_context": {"side": "left"}},
        "error": None,
    }


async def test_a_missing_field_is_a_422_in_the_envelope() -> None:
    status_code, body = await call("POST", "/echo", json={"clinical_context": {}})

    assert status_code == 422
    assert body["data"] is None
    assert body["error"]["code"] == ERROR_CODE_VALIDATION
    assert "cpt_code" in body["error"]["message"]


async def test_the_rejected_value_is_never_echoed_back() -> None:
    """Request bodies here carry patient identifiers. Locations only, no values."""
    status_code, body = await call(
        "POST", "/echo", json={"cpt_code": "72148", "clinical_context": "PATIENT-SECRET-VALUE"}
    )

    assert status_code == 422
    message = body["error"]["message"]
    assert "clinical_context" in message
    assert "PATIENT-SECRET-VALUE" not in message


async def test_a_body_that_is_not_an_object_still_reports_a_location() -> None:
    status_code, body = await call("POST", "/echo", json=[1, 2, 3])

    assert status_code == 422
    assert body["error"]["code"] == ERROR_CODE_VALIDATION
    assert body["error"]["message"].startswith("Request validation failed")


async def test_a_coded_exception_keeps_its_code() -> None:
    status_code, body = await call("GET", "/coded")

    assert status_code == 404
    assert body == {
        "data": None,
        "error": {"code": ERROR_CODE_CUSTOM, "message": "No such session"},
    }


async def test_a_plain_http_exception_falls_back_to_the_generic_code() -> None:
    status_code, body = await call("GET", "/bare")

    assert status_code == 403
    assert body["error"] == {"code": ERROR_CODE_HTTP, "message": "Nope"}


async def test_an_unrouted_path_is_enveloped_too() -> None:
    status_code, body = await call("GET", "/nope")

    assert status_code == 404
    assert body["data"] is None
    assert body["error"]["code"] == ERROR_CODE_HTTP


def test_error_response_builds_the_failure_envelope() -> None:
    response = error_response(503, "unavailable", "Broker unreachable")

    assert response.status_code == 503
    assert response.body == (
        b'{"data":null,"error":{"code":"unavailable","message":"Broker unreachable"}}'
    )


def test_error_responses_declares_the_envelope_for_each_status() -> None:
    declared = error_responses(404, 422)

    assert set(declared) == {404, 422}
    assert all(entry["model"] is ApiResponse[None] for entry in declared.values())
    assert declared[404]["description"] == "The resource does not exist."


def test_error_responses_takes_service_specific_wording() -> None:
    """A route whose 404 means something particular says so, without forking this."""
    declared = error_responses(404, descriptions={404: "The session is unknown."})

    assert declared[404]["description"] == "The session is unknown."


def test_an_undescribed_status_is_a_loud_failure() -> None:
    """Better than publishing a spec with a blank or invented description."""
    with pytest.raises(KeyError, match="418"):
        error_responses(418)


async def test_the_documented_shape_matches_what_is_returned() -> None:
    """The declaration and the handler must not drift apart."""
    app = build_app()
    schema = app.openapi()
    documented = schema["paths"]["/echo"]["post"]["responses"]

    assert "422" in documented

    _, body = await call("POST", "/echo", json={})
    assert set(body) == {"data", "error"}

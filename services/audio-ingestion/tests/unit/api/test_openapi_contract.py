"""The published spec in docs/api must describe the app that actually runs.

CLAUDE.md asks for the spec to be updated whenever a route changes. A convention
alone drifts: this compares the committed YAML against the app's own generated
schema on the parts a client depends on — routes, methods, status codes — without
asserting on wording, so editing a description does not fail the build.

The WebSocket route is outside this comparison by necessity: OpenAPI 3.1 cannot
describe one, so the spec documents it under an `x-websocket-endpoints`
extension. The last two tests here check that the prose at least still names the
things the code does, since nothing else can.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.api.websocket import (
    WS_CLOSE_INTERNAL_ERROR,
    WS_CLOSE_UNAUTHORIZED,
    WS_CLOSE_UNSUPPORTED_DATA,
)
from src.main import create_app

SPEC_PATH = Path(__file__).resolve().parents[5] / "docs" / "api" / "audio-ingestion.yaml"


@pytest.fixture(scope="module")
def published() -> dict[str, Any]:
    return dict(yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def generated() -> dict[str, Any]:
    return dict(create_app().openapi())


def test_the_spec_file_is_where_the_convention_says() -> None:
    assert SPEC_PATH.exists(), f"expected an OpenAPI spec at {SPEC_PATH}"


def test_same_routes_and_methods(published: dict[str, Any], generated: dict[str, Any]) -> None:
    def routes(spec: dict[str, Any]) -> set[tuple[str, str]]:
        return {
            (path, method)
            for path, operations in spec["paths"].items()
            for method in operations
            if method in {"get", "post", "put", "patch", "delete"}
        }

    assert routes(published) == routes(generated)


def test_same_status_codes(published: dict[str, Any], generated: dict[str, Any]) -> None:
    def statuses(spec: dict[str, Any]) -> dict[str, set[str]]:
        return {
            f"{method.upper()} {path}": set(operation["responses"])
            for path, operations in spec["paths"].items()
            for method, operation in operations.items()
        }

    assert statuses(published) == statuses(generated)


def test_health_flags_match_the_model(published: dict[str, Any], generated: dict[str, Any]) -> None:
    published_data = published["components"]["schemas"]["HealthData"]
    generated_data = generated["components"]["schemas"]["HealthData"]

    assert set(published_data["properties"]) == set(generated_data["properties"])
    assert set(published_data["required"]) == set(generated_data["required"])


def test_documented_port_matches_the_local_dev_table(published: dict[str, Any]) -> None:
    assert published["servers"][0]["url"].endswith(":8001")


def test_the_websocket_route_is_documented_as_an_extension(published: dict[str, Any]) -> None:
    """OpenAPI cannot express it, so it must not silently go undocumented."""
    endpoints = published["x-websocket-endpoints"]

    assert "/ws/audio/{session_id}" in endpoints


def test_documented_close_codes_match_the_ones_the_route_sends(
    published: dict[str, Any],
) -> None:
    """The one part of the WebSocket contract a client actually branches on."""
    documented = set(published["x-websocket-endpoints"]["/ws/audio/{session_id}"]["close-codes"])

    assert documented == {
        WS_CLOSE_UNAUTHORIZED,
        WS_CLOSE_UNSUPPORTED_DATA,
        WS_CLOSE_INTERNAL_ERROR,
    }

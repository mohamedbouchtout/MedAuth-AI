"""The published spec in docs/api must describe the app that actually runs.

CLAUDE.md asks for the spec to be updated whenever a route changes. A convention
alone drifts: this compares the committed YAML against the app's own generated
schema on the parts a client depends on — routes, methods, status codes,
required request fields and error codes — without asserting on wording, so
editing a description does not fail the build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from track_a_clinical.api.sessions import (
    ERROR_CODE_SESSION_NOT_FOUND,
    ERROR_CODE_SIGNAL_NOT_PUBLISHED,
)
from track_a_clinical.main import create_app

SPEC_PATH = Path(__file__).resolve().parents[5] / "docs" / "api" / "track-a-clinical.yaml"


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


def test_same_required_request_fields(published: dict[str, Any], generated: dict[str, Any]) -> None:
    published_request = published["components"]["schemas"]["StartSessionRequest"]
    generated_request = generated["components"]["schemas"]["StartSessionRequest"]

    assert set(published_request["required"]) == set(generated_request["required"])
    assert set(published_request["properties"]) == set(generated_request["properties"])


def test_published_request_forbids_unknown_fields(published: dict[str, Any]) -> None:
    """A client-supplied session_id has to be rejected by the documented contract too."""
    request_schema = published["components"]["schemas"]["StartSessionRequest"]

    assert request_schema["additionalProperties"] is False


def test_documented_error_codes_match_the_handlers(published: dict[str, Any]) -> None:
    documented = set(published["components"]["schemas"]["Error"]["properties"]["code"]["enum"])

    assert {ERROR_CODE_SESSION_NOT_FOUND, ERROR_CODE_SIGNAL_NOT_PUBLISHED} <= documented


def test_documented_port_matches_the_local_dev_table(published: dict[str, Any]) -> None:
    assert published["servers"][0]["url"].endswith(":8003")

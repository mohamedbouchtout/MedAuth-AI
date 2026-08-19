"""The published spec in docs/api must describe the app that actually runs.

Same guard track-a-clinical carries: CLAUDE.md asks for the spec to be updated
whenever a route changes, and a convention alone drifts. This compares the
committed YAML against the app's own generated schema on the parts a client
depends on — routes, methods, status codes and the health payload's fields —
without asserting on wording, so editing a description does not fail the build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from track_b_rag.api.envelope import ERROR_CODE_HTTP, ERROR_CODE_VALIDATION
from track_b_rag.main import create_app

SPEC_PATH = Path(__file__).resolve().parents[5] / "docs" / "api" / "track-b-rag.yaml"


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


def test_the_503_is_documented(published: dict[str, Any]) -> None:
    """FastAPI documents only the 200 unless asked; the failure half matters more."""
    assert "503" in published["paths"]["/health"]["get"]["responses"]


def test_same_health_fields(published: dict[str, Any], generated: dict[str, Any]) -> None:
    published_data = published["components"]["schemas"]["HealthData"]
    generated_data = generated["components"]["schemas"]["HealthData"]

    assert set(published_data["required"]) == set(generated_data["required"])
    assert set(published_data["properties"]) == set(generated_data["properties"])


def test_the_component_status_vocabulary_matches(
    published: dict[str, Any], generated: dict[str, Any]
) -> None:
    """ok/error, not healthy/unhealthy — clients switch on these strings.

    The spec factors the pair out as a named ComponentStatus schema; FastAPI
    inlines the same Literal onto each property. Compare the values, not the
    shape they are expressed in.
    """
    assert set(published["components"]["schemas"]["ComponentStatus"]["enum"]) == {"ok", "error"}

    generated_properties = generated["components"]["schemas"]["HealthData"]["properties"]
    for name, schema in generated_properties.items():
        assert set(schema["enum"]) == {"ok", "error"}, name


def test_documented_error_codes_match_the_handlers(published: dict[str, Any]) -> None:
    documented = set(published["components"]["schemas"]["Error"]["properties"]["code"]["enum"])

    assert {ERROR_CODE_VALIDATION, ERROR_CODE_HTTP} <= documented


def test_documented_port_matches_the_local_dev_table(published: dict[str, Any]) -> None:
    assert published["servers"][0]["url"].endswith(":8002")

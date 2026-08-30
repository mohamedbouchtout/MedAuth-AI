"""The published spec in docs/api must describe the app that actually runs.

CLAUDE.md asks for the spec to be updated whenever a route changes. A convention
alone drifts: this compares the committed YAML against the app's own generated
schema on the parts a client depends on — routes, methods, status codes, and the
fields of the models a caller reads — without asserting on wording, so editing a
description does not fail the build.

This is the test that makes `docs/api/fhir-integration.yaml` select this
service's CI job: the spec is half of a contract, so a spec-only edit has to
re-run the check that guards it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.adapters.factory import EHRType
from src.main import create_app

SPEC_PATH = Path(__file__).resolve().parents[5] / "docs" / "api" / "fhir-integration.yaml"


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


def test_same_query_parameters(published: dict[str, Any], generated: dict[str, Any]) -> None:
    """The half of this contract a browser-driven redirect actually depends on."""

    def parameters(spec: dict[str, Any]) -> dict[str, set[tuple[str, bool]]]:
        return {
            f"{method.upper()} {path}": {
                (parameter["name"], bool(parameter.get("required", False)))
                for parameter in operation.get("parameters", [])
            }
            for path, operations in spec["paths"].items()
            for method, operation in operations.items()
        }

    assert parameters(published) == parameters(generated)


def test_health_flags_match_the_model(published: dict[str, Any], generated: dict[str, Any]) -> None:
    published_data = published["components"]["schemas"]["HealthData"]
    generated_data = generated["components"]["schemas"]["HealthData"]

    assert set(published_data["properties"]) == set(generated_data["properties"])
    assert set(published_data["required"]) == set(generated_data["required"])


def test_launch_session_fields_match_the_model(
    published: dict[str, Any], generated: dict[str, Any]
) -> None:
    published_data = published["components"]["schemas"]["LaunchSessionData"]
    generated_data = generated["components"]["schemas"]["LaunchSessionData"]

    assert set(published_data["properties"]) == set(generated_data["properties"])
    assert set(published_data["required"]) == set(generated_data["required"])


def test_the_documented_ehr_vocabulary_is_the_real_one(published: dict[str, Any]) -> None:
    """A published enum that has drifted from EHRType would misdescribe the answer."""
    documented = set(published["components"]["schemas"]["EHRType"]["enum"])

    assert documented == {member.value for member in EHRType}


def test_documented_port_matches_the_local_dev_table(published: dict[str, Any]) -> None:
    assert published["servers"][0]["url"].endswith(":8004")


def test_the_response_never_carries_an_encounter_session_id(published: dict[str, Any]) -> None:
    """CLAUDE.md, "A SMART launch is not an encounter session".

    A ``session_id`` appearing on this service's response models is how the two
    identifiers would quietly become one; the spec is where that would show up
    first, so it is asserted here rather than trusted to review.
    """
    launch_fields = set(published["components"]["schemas"]["LaunchSessionData"]["properties"])

    assert "session_id" not in launch_fields
    assert "launch_id" in launch_fields

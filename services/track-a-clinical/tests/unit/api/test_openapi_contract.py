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

from track_a_clinical.api.notes import (
    ERROR_CODE_NOTE_NOT_GENERATED,
)
from track_a_clinical.api.sessions import (
    ERROR_CODE_AUTH_REJECTED,
    ERROR_CODE_SESSION_COMPLETED,
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


@pytest.mark.parametrize("schema_name", ["StartSessionRequest", "UpdateNoteRequest"])
def test_published_requests_forbid_unknown_fields(
    published: dict[str, Any], schema_name: str
) -> None:
    """A client-supplied session_id, or an attempt to set provider_edited, has to be
    rejected by the documented contract and not only by the running app."""
    request_schema = published["components"]["schemas"][schema_name]

    assert request_schema["additionalProperties"] is False


def test_the_editable_note_fields_are_the_documented_ones(
    published: dict[str, Any], generated: dict[str, Any]
) -> None:
    """Server-owned fields must not appear as editable in either half of the contract."""
    documented = set(published["components"]["schemas"]["UpdateNoteRequest"]["properties"])

    assert documented == set(generated["components"]["schemas"]["UpdateNoteRequest"]["properties"])
    assert not documented & {"provider_edited", "generated_at", "ehr_document_ref_id", "note_id"}


def test_documented_error_codes_match_the_handlers(published: dict[str, Any]) -> None:
    documented = set(published["components"]["schemas"]["Error"]["properties"]["code"]["enum"])

    assert {
        ERROR_CODE_SESSION_NOT_FOUND,
        ERROR_CODE_SIGNAL_NOT_PUBLISHED,
        ERROR_CODE_AUTH_REJECTED,
        ERROR_CODE_SESSION_COMPLETED,
        ERROR_CODE_NOTE_NOT_GENERATED,
    } <= documented


def test_documented_port_matches_the_local_dev_table(published: dict[str, Any]) -> None:
    assert published["servers"][0]["url"].endswith(":8003")

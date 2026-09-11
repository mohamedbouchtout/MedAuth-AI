"""The published spec in docs/api must describe the app that actually runs.

CLAUDE.md asks for the spec to be updated whenever a route changes. A convention
alone drifts: this compares the committed YAML against the app's own generated
schema on the parts a client depends on — routes, methods, status codes — without
asserting on wording, so editing a description does not fail the build.

This service's WebSocket routes are outside that comparison by necessity:
OpenAPI 3.1 cannot describe one, so the spec documents them under an
`x-websocket-endpoints` extension. The last tests here check that the prose at
least still names the things the code does, since nothing else can — and they
compare the documented set against the served set in both directions, so neither
a stale entry nor an undocumented route slips through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from hipaa_logger import AuditAction
from src.api.websocket import WS_CLOSE_INTERNAL_ERROR, WS_CLOSE_UNAUTHORIZED
from src.main import create_app

SPEC_PATH = Path(__file__).resolve().parents[5] / "docs" / "api" / "nudge-service.yaml"

#: Every WebSocket this service serves, by route function name, mapped to the
#: path the spec is expected to document it under. Keyed on the function name so
#: the served path is resolved from the app rather than restated here — a
#: constant asserted against itself proves nothing.
WEBSOCKET_PATHS = {
    "nudge_stream": "/ws/nudges/{session_id}",
    "transcript_stream": "/ws/transcript/{session_id}",
}

#: The audit action each stream writes. Held here so the spec's prose can be
#: checked against the vocabulary rather than against a hardcoded string.
EXPECTED_AUDIT_ACTIONS = {
    "nudge_stream": AuditAction.RELAY_NUDGES,
    "transcript_stream": AuditAction.RELAY_TRANSCRIPT,
}


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
    assert published["servers"][0]["url"].endswith(":8005")


def test_every_websocket_route_is_documented_as_an_extension(published: dict[str, Any]) -> None:
    """OpenAPI cannot express them, so they must not silently go undocumented."""
    documented = set(published["x-websocket-endpoints"])

    assert documented == set(WEBSOCKET_PATHS.values())


def test_the_documented_paths_are_the_ones_the_app_serves(published: dict[str, Any]) -> None:
    """The prose is the only description these routes have; it must name the real paths.

    **Compared as sets, in both directions, and that is a deliberate fix.** This
    test used to read ``next(iter(...))`` and compare the *first* documented
    entry against the nudge route's path. With one endpoint that was equivalent;
    with two it would have gone on passing while asserting nothing whatever
    about the second, because the nudge entry is still written first. A test
    that confirms its own name by coincidence rather than by assertion is worse
    than no test, so it now fails if a documented endpoint is not served or a
    served one is not documented.
    """
    app = create_app()
    served = {app.url_path_for(name, session_id="{session_id}") for name in WEBSOCKET_PATHS}

    assert set(published["x-websocket-endpoints"]) == served


@pytest.mark.parametrize("route_name", sorted(WEBSOCKET_PATHS))
def test_documented_close_codes_match_the_ones_each_route_sends(
    published: dict[str, Any], route_name: str
) -> None:
    """The one part of the WebSocket contract a client actually branches on.

    Parameterised over every stream rather than keyed on a single path constant,
    for the same reason as the test above: both routes go through one
    ``serve_stream`` and send the same two codes, so an entry documenting only
    one of them would have looked complete.
    """
    endpoint = published["x-websocket-endpoints"][WEBSOCKET_PATHS[route_name]]

    assert set(endpoint["close-codes"]) == {WS_CLOSE_UNAUTHORIZED, WS_CLOSE_INTERNAL_ERROR}


@pytest.mark.parametrize("route_name", sorted(WEBSOCKET_PATHS))
def test_every_stream_documents_the_audit_action_it_writes(
    published: dict[str, Any], route_name: str
) -> None:
    """Each stream audits under its own action, and the spec has to say which.

    The two are different disclosures — an encounter's alerts against its speech
    — so a spec that described one stream's audit row and left the other's to be
    inferred would invite exactly the collapse ``RELAY_TRANSCRIPT`` exists to
    prevent.
    """
    endpoint = published["x-websocket-endpoints"][WEBSOCKET_PATHS[route_name]]
    expected_action = EXPECTED_AUDIT_ACTIONS[route_name]

    assert expected_action.value in endpoint["audit"]

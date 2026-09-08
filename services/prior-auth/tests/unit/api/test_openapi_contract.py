"""The published spec in docs/api must describe the app that actually runs.

CLAUDE.md asks for the spec to be updated whenever a route changes. A convention
alone drifts: this compares the committed YAML against the app's own generated
schema on the parts a client depends on — routes, methods, status codes —
without asserting on wording, so editing a description does not fail the build.

The Redis consumer is outside this comparison by necessity: OpenAPI cannot
describe a pub/sub subscriber, so the spec documents it under an
`x-redis-subscriptions` extension. The last tests here check that the prose at
least still names the channels the code subscribes to, since nothing else can.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from prior_auth.consumer import SESSION_ENDED_TEMPLATE, SESSIONS_STARTED_CHANNEL
from prior_auth.main import create_app

SPEC_PATH = Path(__file__).resolve().parents[5] / "docs" / "api" / "prior-auth.yaml"


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
    assert published["servers"][0]["url"].endswith(":8007")


def test_the_subscriptions_are_documented_as_an_extension(published: dict[str, Any]) -> None:
    """OpenAPI cannot express them, so they must not silently go undocumented."""
    documented = set(published["x-redis-subscriptions"])

    assert documented == {SESSIONS_STARTED_CHANNEL, SESSION_ENDED_TEMPLATE}

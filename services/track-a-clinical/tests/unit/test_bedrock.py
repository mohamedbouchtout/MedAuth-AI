"""The two Bedrock models: which region, which model id, and one shared client.

``@mock_aws`` per Known Constraints #3, with the limit track-b-rag's equivalent
suite already states: moto's ``bedrock-runtime`` backend accepts a call and
returns an empty body, so it proves a client is built and addressed correctly
and cannot stand in for a completion. What a model *says* is tested in
``test_soap.py``, where the answers can be chosen.

Nothing here reaches AWS. There are no credentials in CI and none belong there.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from moto import mock_aws

from track_a_clinical import bedrock
from track_a_clinical.config import get_settings


@pytest.fixture(autouse=True)
def clean_clients(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start every test with no cached client, settings or credentials."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("JWT_SIGNING_KEY", "a" * 32)
    get_settings.cache_clear()
    bedrock.reset_clients()
    yield
    bedrock.reset_clients()
    get_settings.cache_clear()


@mock_aws
def test_the_client_is_bedrock_runtime_in_the_configured_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    client = bedrock.get_runtime_client()

    assert client.meta.service_model.service_name == "bedrock-runtime"
    assert client.meta.region_name == "us-east-1"


@mock_aws
def test_the_client_is_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building one resolves credentials and loads service metadata."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert bedrock.get_runtime_client() is bedrock.get_runtime_client()


@mock_aws
def test_both_models_share_one_runtime_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two chat models over one client is one credential resolution, not two."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert bedrock.get_reasoning_model().client is bedrock.get_fast_model().client


@mock_aws
def test_the_model_ids_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never a literal in code — switching models is an environment change."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL_ID_REASONING", "anthropic.claude-sonnet-4-6")
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "anthropic.claude-haiku-4-5-20251001")

    assert bedrock.get_reasoning_model().model_id == "anthropic.claude-sonnet-4-6"
    assert bedrock.get_fast_model().model_id == "anthropic.claude-haiku-4-5-20251001"


@mock_aws
def test_the_two_call_sites_get_different_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE.md's assignment table: Sonnet writes the note, Haiku extracts."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL_ID_REASONING", "reasoning-model")
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "fast-model")

    assert bedrock.get_reasoning_model().model_id != bedrock.get_fast_model().model_id


@mock_aws
def test_decoding_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """A note regenerated from one transcript should not differ by sampling."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert bedrock.get_reasoning_model().temperature == 0.0
    assert bedrock.get_fast_model().temperature == 0.0


@mock_aws
def test_the_note_gets_a_larger_token_budget_than_the_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four sections of prose against a short code list."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert bedrock.get_reasoning_model().max_tokens > bedrock.get_fast_model().max_tokens


@mock_aws
def test_each_model_is_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert bedrock.get_reasoning_model() is bedrock.get_reasoning_model()
    assert bedrock.get_fast_model() is bedrock.get_fast_model()


@mock_aws
def test_resetting_forgets_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    bedrock.get_reasoning_model()
    bedrock.get_fast_model()

    bedrock.reset_clients()

    assert bedrock.get_reasoning_model.cache_info().currsize == 0
    assert bedrock.get_fast_model.cache_info().currsize == 0
    assert bedrock.get_runtime_client.cache_info().currsize == 0


def test_no_client_is_built_by_importing_the_module() -> None:
    """A unit suite that imports a route module must not pay for a boto3 client."""
    assert bedrock.get_runtime_client.cache_info().currsize == 0


async def test_each_invoker_sends_to_its_own_model() -> None:
    """The extraction pass must not quietly end up on the expensive model.

    Patched inside a context of its own so the replacements are undone before
    the autouse fixture's teardown, which calls ``cache_clear`` on the real
    functions.
    """
    used: list[str] = []

    def model(name: str) -> object:
        class FakeModel:
            async def ainvoke(self, messages: object) -> object:
                used.append(name)

                class Response:
                    content = "answer"

                return Response()

        return FakeModel()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bedrock, "get_reasoning_model", lambda: model("reasoning"))
        patch.setattr(bedrock, "get_fast_model", lambda: model("fast"))

        assert await bedrock.invoke_reasoning("prompt") == "answer"
        assert await bedrock.invoke_fast("prompt") == "answer"

    assert used == ["reasoning", "fast"]

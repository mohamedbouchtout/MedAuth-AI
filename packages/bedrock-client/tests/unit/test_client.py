"""Building the clients: which service, which region, which model id.

``@mock_aws`` per Known Constraints #3, with the limit that suite in
``track-b-rag`` already states: moto's ``bedrock-runtime`` backend accepts a
call and returns an empty body, so it proves a client is built and addressed
correctly and cannot stand in for an actual completion. What a model *says* is
tested where the answers can be chosen — against a stub, in the services.

Nothing here reaches AWS. There are no credentials in CI and none belong there.
"""

from __future__ import annotations

from typing import Any

import pytest
from moto import mock_aws

from bedrock_client import build_chat_model, build_runtime_client, invoke


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep boto3 from finding, or looking for, real credentials."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@mock_aws
def test_the_client_is_bedrock_runtime_in_the_requested_region() -> None:
    client = build_runtime_client("us-east-1")

    assert client.meta.service_model.service_name == "bedrock-runtime"
    assert client.meta.region_name == "us-east-1"


@mock_aws
def test_the_region_is_an_argument_not_an_ambient_default() -> None:
    """The caller's settings decide the region; this package never guesses one."""
    assert build_runtime_client("us-west-2").meta.region_name == "us-west-2"


@mock_aws
def test_nothing_is_cached_here() -> None:
    """Caching belongs with the settings that decide when a client is stale."""
    assert build_runtime_client("us-east-1") is not build_runtime_client("us-east-1")


@mock_aws
def test_the_model_id_is_whatever_the_caller_passed() -> None:
    """Never a literal in code — switching models is an environment change."""
    model = build_chat_model(
        client=build_runtime_client("us-east-1"),
        model_id="anthropic.claude-sonnet-4-6",
        temperature=0.0,
        max_tokens=2048,
    )

    assert model.model_id == "anthropic.claude-sonnet-4-6"


@mock_aws
def test_decoding_settings_reach_the_model() -> None:
    model = build_chat_model(
        client=build_runtime_client("us-east-1"),
        model_id="anthropic.claude-haiku-4-5-20251001",
        temperature=0.0,
        max_tokens=512,
    )

    assert model.temperature == 0.0
    assert model.max_tokens == 512


@mock_aws
def test_two_models_can_share_one_runtime_client() -> None:
    """TASK-030 runs Sonnet and Haiku over one encounter; that is one client."""
    client = build_runtime_client("us-east-1")

    fast = build_chat_model(client=client, model_id="fast", temperature=0.0, max_tokens=512)
    reasoning = build_chat_model(
        client=client, model_id="reasoning", temperature=0.0, max_tokens=2048
    )

    assert fast.client is reasoning.client


async def test_invoking_sends_one_human_message_and_returns_its_text() -> None:
    """The contract: prompt in, text out, nothing else in between."""
    sent: list[Any] = []

    class FakeModel:
        async def ainvoke(self, messages: Any) -> Any:
            sent.append(messages)

            class Response:
                content = [{"type": "text", "text": '{"requires_auth": true}'}]

            return Response()

    answer = await invoke(FakeModel(), "what does Aetna require?")  # type: ignore[arg-type]

    assert answer == '{"requires_auth": true}'
    assert len(sent[0]) == 1
    assert sent[0][0].content == "what does Aetna require?"

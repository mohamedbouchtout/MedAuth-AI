"""The Bedrock client: which model, which region, and how a response is read.

``@mock_aws`` per Known Constraints #3, but with a stated limit: moto's
``bedrock-runtime`` backend accepts ``invoke_model`` and returns an empty body,
so it can prove the client is built and addressed correctly and cannot stand in
for an actual completion. The behaviour that depends on what the model *says* —
the retry, the fallback, the parsing — is tested against a stubbed
:func:`track_b_rag.bedrock.invoke_reasoning` in ``test_policy_rules.py``, where
the answers can be chosen.

Nothing here reaches AWS. There are no credentials in CI and none belong there.
"""

from __future__ import annotations

from typing import Any

import pytest
from moto import mock_aws

from track_b_rag import bedrock
from track_b_rag.config import get_settings


@pytest.fixture(autouse=True)
def clean_clients(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Start every test with no cached client, settings or credentials."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
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
def test_the_model_id_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never a literal in code — switching models is an environment change."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL_ID_REASONING", "anthropic.claude-sonnet-4-6")

    model = bedrock.get_reasoning_model()

    assert model.model_id == "anthropic.claude-sonnet-4-6"


@mock_aws
def test_decoding_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two identical cache misses should not disagree about what a payer requires."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert bedrock.get_reasoning_model().temperature == 0.0


@mock_aws
def test_the_model_is_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert bedrock.get_reasoning_model() is bedrock.get_reasoning_model()


@mock_aws
def test_resetting_forgets_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    bedrock.get_reasoning_model()

    bedrock.reset_clients()

    assert bedrock.get_reasoning_model.cache_info().currsize == 0
    assert bedrock.get_runtime_client.cache_info().currsize == 0


def test_no_client_is_built_by_importing_the_module() -> None:
    """A unit suite that imports a route module must not pay for a boto3 client."""
    assert bedrock.get_runtime_client.cache_info().currsize == 0


# --- reading what came back ------------------------------------------------


def test_a_plain_string_response_is_its_own_text() -> None:
    assert bedrock.message_text("just text") == "just text"


def test_a_block_list_is_flattened() -> None:
    """Anthropic models on Bedrock return content blocks, not a bare string.

    A caller assuming the string form silently gets "[{'type': 'text', ...}]" —
    which parses as no JSON at all and would burn the retry on every call.
    """
    content = [{"type": "text", "text": "first "}, {"type": "text", "text": "second"}]

    assert bedrock.message_text(content) == "first second"


def test_non_text_blocks_are_dropped_not_rendered() -> None:
    content = [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "answer"}]

    assert bedrock.message_text(content) == "answer"


def test_a_list_of_bare_strings_is_joined() -> None:
    assert bedrock.message_text(["a", "b"]) == "ab"


def test_anything_else_is_stringified_rather_than_raising() -> None:
    """A shape nobody anticipated becomes an unparseable answer, which is handled."""
    assert bedrock.message_text(42) == "42"


def test_a_text_block_with_no_text_key_contributes_nothing() -> None:
    assert bedrock.message_text([{"type": "text"}]) == ""


async def test_invoking_sends_one_human_message_and_returns_its_text() -> None:
    """The one call site's contract: prompt in, text out, nothing else in between.

    Patched inside a context of its own so the replacement is undone before the
    autouse fixture's teardown, which calls ``cache_clear`` on the real function.
    """
    sent: list[Any] = []

    class FakeModel:
        async def ainvoke(self, messages: Any) -> Any:
            sent.append(messages)

            class Response:
                content = [{"type": "text", "text": '{"requires_auth": true}'}]

            return Response()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bedrock, "get_reasoning_model", FakeModel)
        answer = await bedrock.invoke_reasoning("what does Aetna require?")

    assert answer == '{"requires_auth": true}'
    assert len(sent[0]) == 1
    assert sent[0][0].content == "what does Aetna require?"

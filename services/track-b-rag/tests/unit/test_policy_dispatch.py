"""The seam between a detected procedure and POST /policies/query.

Two halves with very different maturity. The HTTP call is real and fully
exercised here; resolving the parameters it needs is TASK-024's work, and the
tests below pin the fact that it refuses rather than inventing values.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from track_b_rag import policy_dispatch
from track_b_rag.api.schemas import PolicyQueryData
from track_b_rag.config import get_settings
from track_b_rag.keywords import ProcedureMention
from track_b_rag.policy_dispatch import (
    UNRESOLVED_PARAMETERS,
    MissingQueryParameters,
    PolicyQueryParameters,
    post_policy_query,
    resolve_and_query_policy,
    resolve_query_parameters,
)

SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROVIDER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

MENTION = ProcedureMention(
    keyword="MRI",
    procedure="MRI",
    matched_text="MRI",
    excerpt="Conservative therapy failed. Let's order an MRI.",
)

PARAMETERS = PolicyQueryParameters(
    procedure="knee MRI",
    cpt_code="73721",
    payer="Aetna",
    plan_type="PPO",
    state="MA",
    provider_id=PROVIDER_ID,
)

ANSWER = {
    "requires_auth": True,
    "auth_criteria": ["Six weeks of conservative therapy"],
    "missing_criteria": ["Six weeks of conservative therapy"],
    "denial_risk": "high",
    "nudge_message": "Document conservative therapy before ordering.",
    "step_therapy_required": False,
    "step_therapy_details": None,
}


@pytest.fixture(autouse=True)
def clean_settings() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def post_against(
    handler: Callable[[httpx.Request], httpx.Response], **overrides: Any
) -> PolicyQueryData | None:
    """Run :func:`post_policy_query` against a scripted transport."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    kwargs: dict[str, Any] = {
        "parameters": PARAMETERS,
        "session_id": SESSION_ID,
        "clinical_context": {"transcript_excerpt": MENTION.excerpt},
    }
    kwargs.update(overrides)
    httpx.AsyncClient = client  # type: ignore[misc]
    try:
        return await post_policy_query(**kwargs)
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]


class TestResolveQueryParameters:
    """The half that is not built. See TASK-024."""

    async def test_it_refuses_rather_than_inventing_values(self) -> None:
        with pytest.raises(MissingQueryParameters) as raised:
            await resolve_query_parameters(session_id=SESSION_ID, mention=MENTION)

        assert raised.value.fields == UNRESOLVED_PARAMETERS

    async def test_it_names_what_is_missing(self) -> None:
        """The warning has to say what is absent, not just that something is."""
        with pytest.raises(MissingQueryParameters) as raised:
            await resolve_query_parameters(session_id=SESSION_ID, mention=MENTION)

        message = str(raised.value)
        assert "cpt_code" in message
        assert "state" in message

    def test_cpt_code_is_among_them(self) -> None:
        """The one that must never be faked: the rag: cache key is built from it.

        A placeholder code would write a real policy answer under a key standing
        for a different procedure, and the next encounter would be served it.
        """
        assert "cpt_code" in UNRESOLVED_PARAMETERS


class TestPostPolicyQuery:
    """The half that is built: a plain HTTP call to this service's own route."""

    async def test_it_returns_the_parsed_answer(self) -> None:
        answer = await post_against(
            lambda request: httpx.Response(200, json={"data": ANSWER, "error": None})
        )

        assert answer is not None
        assert answer.requires_auth is True
        assert answer.missing_criteria == ["Six weeks of conservative therapy"]

    async def test_it_posts_to_the_query_route_on_the_configured_base_url(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": ANSWER, "error": None})

        await post_against(handler, base_url="http://track-b-rag.svc:8002/")

        assert seen == ["http://track-b-rag.svc:8002/policies/query"]

    async def test_it_sends_every_field_the_route_requires(self) -> None:
        """Including session_id and provider_id, which exist for the audit row."""
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"data": ANSWER, "error": None})

        await post_against(handler)

        (body,) = seen
        assert body == {
            "procedure": "knee MRI",
            "cpt_code": "73721",
            "payer": "Aetna",
            "plan_type": "PPO",
            "state": "MA",
            "clinical_context": {"transcript_excerpt": MENTION.excerpt},
            "session_id": str(SESSION_ID),
            "provider_id": str(PROVIDER_ID),
        }

    @pytest.mark.parametrize(
        "handler",
        [
            pytest.param(lambda request: httpx.Response(500), id="server-error"),
            pytest.param(lambda request: httpx.Response(422), id="rejected"),
            pytest.param(lambda request: httpx.Response(200, content=b"not json"), id="not-json"),
            pytest.param(
                lambda request: httpx.Response(200, json={"data": {"requires_auth": True}}),
                id="incomplete-answer",
            ),
            pytest.param(
                lambda request: httpx.Response(200, json={"data": None, "error": {"code": "x"}}),
                id="error-envelope",
            ),
        ],
    )
    async def test_every_failure_returns_none(
        self, handler: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        assert await post_against(handler) is None

    async def test_a_transport_failure_returns_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        assert await post_against(handler) is None

    async def test_a_failure_is_logged_at_error_without_the_clinical_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The excerpt is PHI; the payer, plan and code are not."""
        await post_against(lambda request: httpx.Response(500))

        assert "73721" in caplog.text
        assert MENTION.excerpt not in caplog.text
        assert caplog.records[-1].levelname == "ERROR"

    async def test_the_default_base_url_comes_from_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POLICY_QUERY_BASE_URL", "http://configured.test:9999")
        get_settings.cache_clear()
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": ANSWER, "error": None})

        await post_against(handler)

        assert seen == ["http://configured.test:9999/policies/query"]


class TestResolveAndQueryPolicy:
    """The single entry point the transcript consumer calls."""

    async def test_it_propagates_the_structural_failure(self) -> None:
        """Swallowing it would hide TASK-024's gap behind a generic None."""
        with pytest.raises(MissingQueryParameters):
            await resolve_and_query_policy(
                session_id=SESSION_ID,
                mention=MENTION,
                clinical_context={"transcript_excerpt": MENTION.excerpt},
            )

    async def test_it_posts_once_the_parameters_resolve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stands in for TASK-024 having landed: the rest of the path already works."""
        seen: list[dict[str, Any]] = []

        async def resolved(**_: Any) -> PolicyQueryParameters:
            return PARAMETERS

        async def posted(**kwargs: Any) -> PolicyQueryData:
            seen.append(kwargs)
            return PolicyQueryData.model_validate(ANSWER)

        monkeypatch.setattr(policy_dispatch, "resolve_query_parameters", resolved)
        monkeypatch.setattr(policy_dispatch, "post_policy_query", posted)

        answer = await resolve_and_query_policy(
            session_id=SESSION_ID,
            mention=MENTION,
            clinical_context={"transcript_excerpt": MENTION.excerpt},
        )

        assert answer is not None
        (call,) = seen
        assert call["parameters"] is PARAMETERS
        assert call["session_id"] == SESSION_ID
        assert call["clinical_context"] == {"transcript_excerpt": MENTION.excerpt}

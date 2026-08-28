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

#: An MRI with no body site stated, so it resolves to no code. Kept as the
#: unqualified case because it is what a clinician most often actually says.
MENTION = ProcedureMention(
    keyword="MRI",
    procedure="MRI",
    matched_text="MRI",
    excerpt="Conservative therapy failed. Let's order an MRI.",
)

#: The same keyword with a site, so a code resolves and the happy path is reachable.
KNEE_MENTION = ProcedureMention(
    keyword="MRI",
    procedure="MRI",
    matched_text="MRI",
    excerpt="The knee has been locking. Let's order an MRI of the knee.",
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
    "source": "rag",
}


@pytest.fixture(autouse=True)
def clean_settings() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


#: Distinguishes "build a row from the column arguments" from an explicit None,
#: which means "no such encounter".
_MISSING: Any = object()


class _Row:
    """The four non-patient columns the real SELECT asks for, and nothing else.

    Deliberately not an ``Encounter``: a fake carrying ``patient_fhir_id`` would
    let a test pass against a SELECT that had quietly started reading PHI.
    """

    def __init__(self, payer: str, plan_type: str, state: str) -> None:
        self.provider_id = PROVIDER_ID
        self.insurance_payer = payer
        self.insurance_plan_type = plan_type
        self.state = state


def _forbidden_sessionmaker() -> Any:
    """Stand in for the sessionmaker on paths that must not reach the database."""
    raise AssertionError("the database was queried before the procedure code resolved")


def fake_encounter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payer: str = "Aetna",
    plan_type: str = "PPO",
    state: str = "MA",
    row: _Row | None | Any = _MISSING,
    raises: Exception | None = None,
) -> None:
    """Point :func:`resolve_query_parameters` at a scripted encounter row.

    Args:
        monkeypatch: pytest's patcher.
        payer, plan_type, state: The columns to report as populated. An empty
            string stands for a NULL column, which is what every encounter has
            until TASK-052b.
        row: Pass ``None`` for "no such encounter". Omit it to build a row from
            the three column arguments.
        raises: Raise this from ``execute`` instead, for the transient-failure path.
    """
    resolved = _Row(payer, plan_type, state) if row is _MISSING else row

    class FakeResult:
        def one_or_none(self) -> Any:
            return resolved

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def execute(self, statement: Any) -> FakeResult:
            if raises is not None:
                raise raises
            return FakeResult()

    monkeypatch.setattr(policy_dispatch, "get_sessionmaker", lambda: FakeSession, raising=True)


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
    """Resolving a mention plus an encounter row into a query.

    The encounter read is faked here so the branches can be driven directly;
    ``tests/integration/test_query_parameters.py`` runs the same function against
    a real row, which is what proves the column and the SELECT agree.
    """

    async def test_it_builds_a_query_from_a_fully_populated_encounter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What TASK-052b will make true for real encounters."""
        fake_encounter(monkeypatch, payer="Aetna", plan_type="PPO", state="MA")

        parameters = await resolve_query_parameters(session_id=SESSION_ID, mention=KNEE_MENTION)

        assert parameters.cpt_code == "73721"
        assert parameters.payer == "Aetna"
        assert parameters.plan_type == "PPO"
        assert parameters.state == "MA"
        assert parameters.provider_id == PROVIDER_ID

    async def test_the_payer_keeps_its_own_spelling_for_the_route_to_normalise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One normalisation site, and it is the route.

        `/policies/query` resolves the slug through payer_vocab and warns on an
        unknown one. Normalising here as well would put that rule in two places.
        """
        fake_encounter(monkeypatch, payer="Medicare Part B", plan_type="HMO", state="MA")

        parameters = await resolve_query_parameters(session_id=SESSION_ID, mention=KNEE_MENTION)

        assert parameters.payer == "Medicare Part B"

    @pytest.mark.parametrize(
        ("column", "expected"),
        [
            ("payer", ("payer",)),
            ("plan_type", ("plan_type",)),
            ("state", ("state",)),
        ],
    )
    async def test_it_names_only_the_columns_actually_empty(
        self, monkeypatch: pytest.MonkeyPatch, column: str, expected: tuple[str, ...]
    ) -> None:
        """The raise reports this encounter's gap, not a fixed list.

        A static list would keep saying "payer, plan_type, state" while TASK-052b
        filled them in one at a time, and the log would stop meaning anything.
        """
        populated = {"payer": "Aetna", "plan_type": "PPO", "state": "MA"}
        populated[column] = ""
        fake_encounter(monkeypatch, **populated)

        with pytest.raises(MissingQueryParameters) as raised:
            await resolve_query_parameters(session_id=SESSION_ID, mention=KNEE_MENTION)

        assert raised.value.fields == expected

    async def test_an_unmappable_procedure_refuses_before_touching_the_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No code, no query — and the reason says which kind of "no" it was."""
        monkeypatch.setattr(
            policy_dispatch, "get_sessionmaker", _forbidden_sessionmaker, raising=True
        )

        with pytest.raises(MissingQueryParameters) as raised:
            await resolve_query_parameters(session_id=SESSION_ID, mention=MENTION)

        assert raised.value.fields == ("cpt_code",)
        assert raised.value.reason is not None
        assert "qualifier_not_stated" in raised.value.reason

    async def test_an_unknown_encounter_is_structural_rather_than_transient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A soft-deleted or absent encounter will not become present by waiting.

        TASK-006 writes the row before it announces the session, so a subscriber
        cannot race ahead of it.
        """
        fake_encounter(monkeypatch, row=None)

        with pytest.raises(MissingQueryParameters) as raised:
            await resolve_query_parameters(session_id=SESSION_ID, mention=KNEE_MENTION)

        assert raised.value.reason == "no active encounter for this session"

    async def test_a_database_failure_is_not_turned_into_a_structural_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The consumer releases the dedup claim for a transient failure only.

        Wrapping a dropped connection in MissingQueryParameters would keep the
        claim and silence every later mention of that procedure for the visit.
        """
        fake_encounter(monkeypatch, raises=ConnectionResetError("pool is gone"))

        with pytest.raises(ConnectionResetError):
            await resolve_query_parameters(session_id=SESSION_ID, mention=KNEE_MENTION)

    def test_the_still_unresolved_list_is_what_task_052b_populates(self) -> None:
        """cpt_code and provider_id have left this list; the payer columns have not."""
        assert UNRESOLVED_PARAMETERS == ("payer", "plan_type", "state")


class TestProcedureKey:
    """The dedup identity, moved from the keyword to the code by TASK-024."""

    def test_a_resolvable_procedure_is_claimed_on_its_code(self) -> None:
        assert policy_dispatch.procedure_key(KNEE_MENTION) == "cpt:73721"

    def test_an_unresolvable_one_falls_back_to_the_keyword(self) -> None:
        assert policy_dispatch.procedure_key(MENTION) == "keyword:MRI"

    def test_two_keywords_for_one_code_share_a_claim(self) -> None:
        """The behaviour TASK-021 could not have: one order, one nudge.

        A knee and a hip MRI are both 73721, so a visit naming both raises one
        nudge. That is correct — it is one authorization question — and it is
        what the opaque procedure_key parameter was left open for.
        """
        hip = ProcedureMention(
            keyword="MRI",
            procedure="MRI",
            matched_text="MRI",
            excerpt="Let's get an MRI of the hip.",
        )

        assert policy_dispatch.procedure_key(KNEE_MENTION) == policy_dispatch.procedure_key(hip)

    def test_a_code_claim_can_never_collide_with_a_keyword_claim(self) -> None:
        """Prefixes, so the set read out of Redis says which kind each member is."""
        assert policy_dispatch.procedure_key(KNEE_MENTION).startswith("cpt:")
        assert policy_dispatch.procedure_key(MENTION).startswith("keyword:")


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

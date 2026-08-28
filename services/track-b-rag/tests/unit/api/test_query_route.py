"""POST /policies/query — the HTTP contract, and the audit row it must write.

The pipeline itself is faked out; ``tests/unit/test_policy_rules.py`` and
``tests/unit/test_query.py`` cover the two stages and
``tests/integration/test_policy_query.py`` covers them against real stores. What
is left here is the contract: the envelope, what validates and what does not,
and the standing decision that this route — unlike ``/policies/ingest`` next
door — writes an audit row, with identifiers in it and nothing else.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient

from track_b_rag.api import query as query_route
from track_b_rag.query import PolicyQueryAnswer, fallback_answer

SESSION_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
PROVIDER_ID = "9c858901-8a57-4791-81fe-4c455b099bc9"


def body(**overrides: Any) -> dict[str, Any]:
    """A well-formed request, with whatever a test needs changed."""
    payload: dict[str, Any] = {
        "procedure": "knee MRI",
        "cpt_code": "73721",
        "payer": "Aetna",
        "plan_type": "PPO",
        "state": "MA",
        "clinical_context": {"hpi": "knee pain after a fall"},
        "session_id": SESSION_ID,
        "provider_id": PROVIDER_ID,
    }
    payload.update(overrides)
    return payload


class Recorder:
    """Captures what the route asked the pipeline for, and what it audited."""

    def __init__(self) -> None:
        self.query_kwargs: dict[str, Any] | None = None
        self.audits: list[dict[str, Any]] = []
        self.answer = PolicyQueryAnswer(
            requires_auth=True,
            auth_criteria=["Failed six weeks of conservative therapy"],
            missing_criteria=["Documented neurological deficit on examination"],
            denial_risk="medium",
            nudge_message="Prior authorization required for knee MRI.",
            step_therapy_required=False,
            step_therapy_details=None,
            source="rag",
        )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[Recorder]:
    captured = Recorder()

    async def fake_answer(**kwargs: Any) -> PolicyQueryAnswer:
        captured.query_kwargs = kwargs
        return captured.answer

    async def fake_audit(**kwargs: Any) -> None:
        captured.audits.append(kwargs)

    monkeypatch.setattr(query_route.query, "answer_policy_query", fake_answer)
    monkeypatch.setattr(query_route.audit, "audit_policy_query", fake_audit)
    yield captured


# --- the envelope ----------------------------------------------------------


async def test_a_query_returns_the_standard_envelope(
    client: AsyncClient, recorder: Recorder
) -> None:
    response = await client.post("/policies/query", json=body())

    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "requires_auth": True,
            "auth_criteria": ["Failed six weeks of conservative therapy"],
            "missing_criteria": ["Documented neurological deficit on examination"],
            "denial_risk": "medium",
            "nudge_message": "Prior authorization required for knee MRI.",
            "step_therapy_required": False,
            "step_therapy_details": None,
            "policy_source": None,
            "source": "rag",
        },
        "error": None,
    }


async def test_the_response_says_which_tier_answered(
    client: AsyncClient, recorder: Recorder
) -> None:
    """TASK-040 reverses this deliberately, and only for one distinction.

    This test previously asserted `"source" not in data`, on the grounds that a
    caller branching on the answering path would be reading something that is
    not a contract. That reasoning still holds for cache vs rag vs crd, and the
    field's own description says so.

    What it got wrong is that `fallback` is not one of those. It is the
    difference between an answer and the absence of one: the empty
    `auth_criteria` means "unknown", not "none". TASK-040's emitter has to know,
    so it can withhold the haptic escalation on an answer nothing verified, and
    the alternative was inferring it from "high risk and no criteria" — a guess
    at this field rather than a reading of it, and a fourth place deriving a
    decision this work is busy reducing to one.
    """
    response = await client.post("/policies/query", json=body())

    assert response.json()["data"]["source"] == "rag"


async def test_a_fallback_answer_says_it_is_a_fallback(
    client: AsyncClient, recorder: Recorder
) -> None:
    """The one distinction a caller may act on, over the wire.

    TASK-040's emitter reads this to keep an unverifiable answer from firing the
    haptic escalation. Asserted at the route rather than only on
    ``PolicyQueryAnswer`` because the emitter is an HTTP client: a ``source``
    that stopped crossing the boundary would leave it silently inferring again.
    """
    recorder.answer = fallback_answer()

    response = await client.post("/policies/query", json=body())

    data = response.json()["data"]
    assert data["source"] == "fallback"
    assert data["denial_risk"] == "high"
    assert data["auth_criteria"] == []


async def test_every_field_reaches_the_pipeline(client: AsyncClient, recorder: Recorder) -> None:
    await client.post("/policies/query", json=body())

    assert recorder.query_kwargs is not None
    assert recorder.query_kwargs["procedure"] == "knee MRI"
    assert recorder.query_kwargs["cpt_code"] == "73721"
    assert recorder.query_kwargs["payer"] == "aetna"  # normalised, see TASK-016
    assert recorder.query_kwargs["plan_type"] == "PPO"
    assert recorder.query_kwargs["state"] == "MA"
    assert recorder.query_kwargs["clinical_context"] == {"hpi": "knee pain after a fall"}


async def test_the_payer_is_normalised_before_the_pipeline_sees_it(
    client: AsyncClient, recorder: Recorder
) -> None:
    """The whole point of TASK-016: a Coverage display name has to reach the same
    slug the ingest wrote, or retrieval filters on a string nothing was indexed
    under and the caller cannot tell that from "no policy on file"."""
    await client.post("/policies/query", json=body(payer="Medicare Part B"))

    assert recorder.query_kwargs is not None
    assert recorder.query_kwargs["payer"] == "cms-medicare"


@pytest.mark.parametrize("spelling", ["Aetna", "AETNA", "aetna, inc.", "  Aetna Inc  "])
async def test_every_spelling_of_one_payer_queries_the_same_slug(
    client: AsyncClient, recorder: Recorder, spelling: str
) -> None:
    """One slug means one Qdrant filter value and one `rag:` cache key."""
    await client.post("/policies/query", json=body(payer=spelling))

    assert recorder.query_kwargs is not None
    assert recorder.query_kwargs["payer"] == "aetna"


async def test_an_unrecognised_payer_is_answered_and_logged(
    client: AsyncClient, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """Not an error — it answers. The WARNING is what keeps "the name did not line
    up" distinguishable from "this payer has no policy on file"."""
    with caplog.at_level("WARNING", logger="track_b_rag.api.query"):
        response = await client.post(
            "/policies/query", json=body(payer="Sierra Valley Regional Health Plan")
        )

    assert response.status_code == 200
    assert recorder.query_kwargs is not None
    assert recorder.query_kwargs["payer"] == "sierra-valley-regional-health-plan"
    assert "Sierra Valley Regional Health Plan" in caplog.text
    assert "sierra-valley-regional-health-plan" in caplog.text


async def test_a_known_payer_logs_no_warning(
    client: AsyncClient, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="track_b_rag.api.query"):
        await client.post("/policies/query", json=body(payer="Medicare Part B"))

    assert caplog.text == ""


async def test_a_payer_name_with_no_slug_is_a_422(client: AsyncClient, recorder: Recorder) -> None:
    """Caught at the boundary, so the field is named rather than failing deeper in."""
    response = await client.post("/policies/query", json=body(payer="---"))

    assert response.status_code == 422
    assert recorder.query_kwargs is None


async def test_the_configured_collection_is_used(
    client: AsyncClient, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QDRANT_COLLECTION", "policies_staging")

    await client.post("/policies/query", json=body())

    assert recorder.query_kwargs is not None
    assert recorder.query_kwargs["collection"] == "policies_staging"


async def test_a_fallback_answer_is_still_a_200(client: AsyncClient, recorder: Recorder) -> None:
    """TASK-021 fires nudges mid-encounter; an error there reads as silence."""
    from track_b_rag.query import fallback_answer

    recorder.answer = fallback_answer()

    response = await client.post("/policies/query", json=body())

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["denial_risk"] == "high"
    assert data["nudge_message"] == (
        "Unable to verify authorization requirements — confirm manually"
    )


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    ["procedure", "cpt_code", "payer", "plan_type", "state", "session_id", "provider_id"],
)
async def test_the_required_fields_are_required(
    client: AsyncClient, recorder: Recorder, missing: str
) -> None:
    payload = body()
    del payload[missing]

    response = await client.post("/policies/query", json=payload)

    assert response.status_code == 422


async def test_clinical_context_may_be_omitted(client: AsyncClient, recorder: Recorder) -> None:
    """A keyword heard with no surrounding context yet is still worth answering."""
    payload = body()
    del payload["clinical_context"]

    response = await client.post("/policies/query", json=payload)

    assert response.status_code == 200
    assert recorder.query_kwargs is not None
    assert recorder.query_kwargs["clinical_context"] == {}


async def test_a_state_code_is_normalised_to_uppercase(
    client: AsyncClient, recorder: Recorder
) -> None:
    """Ingestion uppercases too, which is what makes the filter and the key agree."""
    await client.post("/policies/query", json=body(state="ma"))

    assert recorder.query_kwargs is not None
    assert recorder.query_kwargs["state"] == "MA"


async def test_a_cpt_code_is_normalised_to_uppercase(
    client: AsyncClient, recorder: Recorder
) -> None:
    """HCPCS codes carry a letter — 'g0179' and 'G0179' are one code, one cache key."""
    await client.post("/policies/query", json=body(cpt_code="g0179"))

    assert recorder.query_kwargs is not None
    assert recorder.query_kwargs["cpt_code"] == "G0179"


@pytest.mark.parametrize("state", ["M", "MASS", "12", ""])
async def test_a_malformed_state_code_is_rejected(
    client: AsyncClient, recorder: Recorder, state: str
) -> None:
    assert (await client.post("/policies/query", json=body(state=state))).status_code == 422


async def test_a_session_id_that_is_not_a_uuid_is_rejected(
    client: AsyncClient, recorder: Recorder
) -> None:
    """It goes into an audit column typed UUID; catching it here beats catching it there."""
    response = await client.post("/policies/query", json=body(session_id="not-a-uuid"))

    assert response.status_code == 422


async def test_an_unknown_field_is_rejected(client: AsyncClient, recorder: Recorder) -> None:
    response = await client.post("/policies/query", json=body(patient_name="Zebulon"))

    assert response.status_code == 422


async def test_a_validation_failure_uses_the_envelope(
    client: AsyncClient, recorder: Recorder
) -> None:
    body_json = (await client.post("/policies/query", json={})).json()

    assert body_json["data"] is None
    assert body_json["error"]["code"] == "validation_error"


async def test_a_validation_failure_never_echoes_the_rejected_value(
    client: AsyncClient, recorder: Recorder
) -> None:
    """The bodies this route receives carry clinical context and patient identifiers."""
    response = await client.post(
        "/policies/query", json=body(state="Zebulon Quackenbush, MRN-8675309")
    )

    assert "Quackenbush" not in response.text
    assert "8675309" not in response.text


# --- the audit row ---------------------------------------------------------


async def test_the_route_writes_an_audit_row(client: AsyncClient, recorder: Recorder) -> None:
    """Known Constraints #6: audit if and only if the route touches PHI. This does."""
    await client.post("/policies/query", json=body())

    assert len(recorder.audits) == 1


async def test_the_audit_row_names_the_session_and_the_provider(
    client: AsyncClient, recorder: Recorder
) -> None:
    await client.post("/policies/query", json=body())

    audited = recorder.audits[0]
    assert audited["session_id"] == uuid.UUID(SESSION_ID)
    assert audited["provider_id"] == uuid.UUID(PROVIDER_ID)


async def test_the_audit_row_carries_no_clinical_detail(
    client: AsyncClient, recorder: Recorder
) -> None:
    """hipaa-logger records that an access happened, never what was accessed."""
    await client.post(
        "/policies/query",
        json=body(clinical_context={"hpi": "fell while skiing", "mrn": "MRN-8675309"}),
    )

    audited = str(recorder.audits[0])
    assert "skiing" not in audited
    assert "8675309" not in audited
    assert "73721" not in audited
    assert "knee MRI" not in audited


async def test_the_audit_row_carries_the_caller_metadata(
    client: AsyncClient, recorder: Recorder
) -> None:
    await client.post(
        "/policies/query", json=body(), headers={"user-agent": "track-b-rag-consumer/1.0"}
    )

    assert recorder.audits[0]["user_agent"] == "track-b-rag-consumer/1.0"


async def test_the_audit_is_written_before_the_work_begins(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An access that cannot be recorded must not proceed.

    hipaa-logger never suppresses a failed write, so auditing first means the
    request fails instead of quietly reading PHI it could not account for.
    """
    order: list[str] = []

    async def failing_audit(**kwargs: Any) -> None:
        order.append("audit")
        raise RuntimeError("audit_log is down")

    async def fake_answer(**kwargs: Any) -> PolicyQueryAnswer:
        order.append("query")
        raise AssertionError("the pipeline ran despite an unrecordable access")

    monkeypatch.setattr(query_route.audit, "audit_policy_query", failing_audit)
    monkeypatch.setattr(query_route.query, "answer_policy_query", fake_answer)

    with pytest.raises(RuntimeError, match="audit_log is down"):
        await client.post("/policies/query", json=body())

    assert order == ["audit"]


def test_the_route_module_has_an_audit_call_site() -> None:
    """The mirror of the ingest route's guard, in the direction that matters here.

    Checks executable code rather than the file text, so the module docstring's
    explanation of why this route audits cannot satisfy the test on its own.
    """
    import ast

    with open(query_route.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "audit_policy_query" in called

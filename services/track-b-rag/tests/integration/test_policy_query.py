"""TASK-012's claims against a real Qdrant, a real Redis and a real Postgres.

Three of them cannot be proven against fakes:

* **The cache holds payer-policy fields and nothing else.** That is a statement
  about the bytes sitting in Redis, and a fake reports whatever it was told.
* **Two patients on one plan share the criteria and not the gaps.** This is the
  correctness test for the two-stage split — the design exists because caching
  the whole response would serve patient B the documentation gaps computed for
  patient A. It asserts both halves at once: Stage 1 is served from cache (the
  model is called once across two queries) *and* ``missing_criteria`` differs
  between the two answers. A single cached blob passes neither half. Do not
  "simplify" this test into one that only checks the cache hit.
* **The audit row lands.** ``audit_log`` is a real table with real constraints,
  and "the route called a function" is not the same claim as "a row exists".

The embedding model is stubbed with deterministic vectors of the right width,
as TASK-011's integration suite does: what is under test is the query pipeline's
use of the two stores, not embedding quality, and the real 1.3 GB weights would
put these behind ``RUN_EMBEDDING_TESTS`` and leave the claims above unverified
on most runs. Bedrock is stubbed for the same reason and one more — moto's
``bedrock-runtime`` returns an empty body, so it cannot stand in for a
completion, and CI holds no credentials for the real thing.

Each test uses its own collection and its own payer, so the cache keys it writes
belong to it alone and a shared CI container is not left dirty for the next
member.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from qdrant_client import QdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import hipaa_logger
from payer_vocab import normalize_payer
from track_b_rag import bedrock, embeddings, vector_store
from track_b_rag.api.dependencies import get_qdrant, get_redis
from track_b_rag.config import get_settings
from track_b_rag.main import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("QDRANT_HOST")
        or not os.environ.get("REDIS_URL")
        or not os.environ.get("DATABASE_URL"),
        reason="needs a real Qdrant, Redis and PostgreSQL (QDRANT_HOST, REDIS_URL, DATABASE_URL)",
    ),
]

VECTOR_SIZE = 1024

CRITERIA = [
    "Failed six weeks of conservative therapy",
    "Documented neurological deficit on examination",
]

ANSWER = json.dumps(
    {
        "requires_auth": True,
        "auth_criteria": CRITERIA,
        "step_therapy_required": False,
        "step_therapy_details": None,
    }
)

#: The two halves of the response, named here so the assertions about which is
#: cacheable read as statements rather than as string literals.
PAYER_POLICY_FIELDS = {
    "requires_auth",
    "auth_criteria",
    "step_therapy_required",
    "step_therapy_details",
}
PATIENT_SPECIFIC_FIELDS = {"missing_criteria", "denial_risk", "nudge_message"}


class BedrockStub:
    """Stands in for Sonnet, counting calls and returning scripted answers."""

    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = answers or [ANSWER]
        self.prompts: list[str] = []

    async def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self.answers) - 1)
        return self.answers[index]

    @property
    def calls(self) -> int:
        return len(self.prompts)


@pytest.fixture
def payer() -> str:
    """A payer name unique to one test, so its cache keys are its own."""
    return f"TestPayer-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def collection(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A collection of this test's own, created and dropped around it."""
    name = f"test_query_{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("QDRANT_COLLECTION", name)
    get_settings.cache_clear()

    client = _qdrant()
    vector_store.ensure_collection(client, name, VECTOR_SIZE)
    vector_store.ensure_payload_indexes(client, name)
    try:
        yield name
    finally:
        client.delete_collection(name)
        client.close()
        get_settings.cache_clear()


@pytest.fixture
def stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic vectors of the right width, in place of 1.3 GB of weights."""
    monkeypatch.setattr(embeddings, "embed_query", lambda text: [0.1] * VECTOR_SIZE)
    monkeypatch.setattr(
        embeddings, "embed_documents", lambda texts: [[0.1] * VECTOR_SIZE for _ in texts]
    )


@pytest.fixture
def bedrock_stub(monkeypatch: pytest.MonkeyPatch) -> BedrockStub:
    stub = BedrockStub()
    monkeypatch.setattr(bedrock, "invoke_reasoning", stub.invoke)
    return stub


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(os.environ["REDIS_URL"])
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def audit_pool() -> AsyncIterator[None]:
    """Close hipaa-logger's pool after each test.

    The pool binds to the event loop that created it, and pytest-asyncio gives
    each test its own — a pool left open would be reused from a loop it does not
    belong to.
    """
    yield
    await hipaa_logger.close_pool()


@pytest_asyncio.fixture
async def client(
    collection: str,
    redis: Redis,
    stub_embedder: None,
    bedrock_stub: BedrockStub,
    audit_pool: None,
) -> AsyncIterator[AsyncClient]:
    """The app, wired to the real Qdrant and the real Redis."""
    qdrant = _qdrant()
    app = create_app()
    app.dependency_overrides[get_qdrant] = lambda: qdrant
    app.dependency_overrides[get_redis] = lambda: redis
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://track-b-rag") as http:
            yield http
    finally:
        qdrant.close()


@pytest.fixture
def index_policy(collection: str) -> Callable[..., None]:
    """Write policy chunks into the collection the way ingestion would."""

    def index(
        *,
        payer: str,
        text: str,
        policy_id: str | None = None,
        state: str | None = "MA",
        plan_type: str | None = "PPO",
    ) -> None:
        client = _qdrant()
        try:
            points = vector_store.build_points(
                policy_id=policy_id or f"POL-{uuid.uuid4().hex[:8]}",
                # Ingestion indexes under the slug, and this fixture exists to do
                # what ingestion does — indexing the display name here would make
                # every retrieval in this module test a filter production never uses.
                payer=normalize_payer(payer),
                plan_type=plan_type,
                state=state,
                chunks=[text],
                vectors=[[0.1] * VECTOR_SIZE],
            )
            vector_store.upsert_points(client, collection, points)
        finally:
            client.close()

    return index


def _qdrant() -> QdrantClient:
    return QdrantClient(
        host=os.environ["QDRANT_HOST"],
        port=int(os.environ.get("QDRANT_PORT", "6333")),
    )


def request_body(payer: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "procedure": "knee MRI",
        "cpt_code": "73721",
        "payer": payer,
        "plan_type": "PPO",
        "state": "MA",
        "clinical_context": {},
        "session_id": str(uuid.uuid4()),
        "provider_id": str(uuid.uuid4()),
    }
    payload.update(overrides)
    return payload


def cache_key(payer: str) -> str:
    """The documented key, whose payer segment is the slug (TASK-016)."""
    return f"rag:{normalize_payer(payer)}:PPO:MA:73721"


# --- the query path --------------------------------------------------------


async def test_a_query_is_answered_from_indexed_policy_text(
    client: AsyncClient,
    index_policy: Callable[..., None],
    payer: str,
    bedrock_stub: BedrockStub,
) -> None:
    """TASK-012's first test: knee MRI on an Aetna-style PPO returns structured JSON."""
    index_policy(payer=payer, text="Prior authorization is required for MRI of the knee.")

    response = await client.post("/policies/query", json=request_body(payer))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["requires_auth"] is True
    assert data["auth_criteria"] == CRITERIA
    assert data["denial_risk"] in {"low", "medium", "high"}
    assert bedrock_stub.calls == 1


async def test_the_prompt_holds_the_retrieved_policy_text(
    client: AsyncClient,
    index_policy: Callable[..., None],
    payer: str,
    bedrock_stub: BedrockStub,
) -> None:
    index_policy(payer=payer, text="Knee MRI requires six weeks of conservative therapy.")

    await client.post("/policies/query", json=request_body(payer))

    assert "six weeks of conservative therapy" in bedrock_stub.prompts[0]


async def test_another_payers_policy_is_not_retrieved(
    client: AsyncClient,
    index_policy: Callable[..., None],
    payer: str,
    bedrock_stub: BedrockStub,
) -> None:
    """The payer filter is what keeps one insurer's rules out of another's answer."""
    index_policy(payer=payer, text="This payer requires conservative therapy first.")
    index_policy(payer=f"Other-{uuid.uuid4().hex[:8]}", text="A different insurer entirely.")

    await client.post("/policies/query", json=request_body(payer))

    assert "conservative therapy first" in bedrock_stub.prompts[0]
    assert "A different insurer entirely" not in bedrock_stub.prompts[0]


async def test_a_differently_spelled_payer_still_retrieves_its_policy(
    client: AsyncClient,
    index_policy: Callable[..., None],
    payer: str,
    bedrock_stub: BedrockStub,
) -> None:
    """TASK-016 end to end, through the route and the real Qdrant filter.

    A document indexed under one spelling is asked about under another — the
    situation every query is in, since the caller's payer comes from a FHIR
    Coverage display name and the corpus was ingested from a payer's own
    publication. Before the shared vocabulary these were two payers, the search
    matched nothing, and the answer was the fallback, which reads exactly like a
    payer whose policies we do not have.

    The spellings are derived from the per-test payer rather than hardcoded, so
    this test keeps its own cache key like every other one in this module.
    """
    index_policy(payer=payer, text="Lumbar MRI requires six weeks of conservative therapy.")

    await client.post("/policies/query", json=request_body(f"{payer.upper()}, Inc."))

    assert bedrock_stub.prompts, "the query never reached Bedrock, so retrieval found nothing"
    assert "six weeks of conservative therapy" in bedrock_stub.prompts[0]


async def test_a_national_policy_is_retrieved_alongside_the_state_one(
    client: AsyncClient,
    index_policy: Callable[..., None],
    payer: str,
    bedrock_stub: BedrockStub,
) -> None:
    """A policy ingested with no state applies everywhere — CMS publishes many."""
    index_policy(payer=payer, text="Massachusetts-specific guidance.", state="MA")
    index_policy(payer=payer, text="National coverage determination text.", state=None)

    await client.post("/policies/query", json=request_body(payer))

    assert "Massachusetts-specific guidance" in bedrock_stub.prompts[0]
    assert "National coverage determination text" in bedrock_stub.prompts[0]


async def test_nothing_indexed_answers_with_the_fallback(
    client: AsyncClient, payer: str, bedrock_stub: BedrockStub
) -> None:
    """No policy text is not "no authorization needed" — it is "we do not know"."""
    response = await client.post("/policies/query", json=request_body(payer))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["requires_auth"] is True
    assert data["denial_risk"] == "high"
    assert data["nudge_message"] == (
        "Unable to verify authorization requirements — confirm manually"
    )
    assert bedrock_stub.calls == 0


# --- what the cache holds --------------------------------------------------


async def test_the_answer_is_cached_under_the_documented_key(
    client: AsyncClient, index_policy: Callable[..., None], payer: str, redis: Redis
) -> None:
    index_policy(payer=payer, text="Prior authorization required.")

    await client.post("/policies/query", json=request_body(payer))

    assert await redis.get(cache_key(payer)) is not None


async def test_the_cached_entry_expires_within_a_day(
    client: AsyncClient, index_policy: Callable[..., None], payer: str, redis: Redis
) -> None:
    index_policy(payer=payer, text="Prior authorization required.")

    await client.post("/policies/query", json=request_body(payer))

    ttl = await redis.ttl(cache_key(payer))
    assert 0 < ttl <= 86_400


async def test_the_cache_holds_payer_fields_and_no_patient_fields(
    client: AsyncClient, index_policy: Callable[..., None], payer: str, redis: Redis
) -> None:
    """The bytes in Redis, inspected directly. This is the invariant, not a proxy for it."""
    index_policy(payer=payer, text="Prior authorization required.")

    await client.post(
        "/policies/query",
        json=request_body(payer, clinical_context={"hpi": "fell while skiing", "mrn": "MRN-867"}),
    )

    raw = await redis.get(cache_key(payer))
    assert raw is not None
    cached = json.loads(raw)
    assert set(cached) == PAYER_POLICY_FIELDS
    assert not set(cached) & PATIENT_SPECIFIC_FIELDS
    assert "skiing" not in raw.decode("utf-8")
    assert "MRN-867" not in raw.decode("utf-8")


async def test_a_second_identical_query_is_served_from_cache(
    client: AsyncClient,
    index_policy: Callable[..., None],
    payer: str,
    bedrock_stub: BedrockStub,
) -> None:
    """TASK-012's second test: the expensive half is paid for once."""
    index_policy(payer=payer, text="Prior authorization required.")
    body = request_body(payer)

    first = await client.post("/policies/query", json=body)
    second = await client.post("/policies/query", json=body)

    assert first.json()["data"] == second.json()["data"]
    assert bedrock_stub.calls == 1


# --- the correctness test for the two-stage split --------------------------


async def test_two_patients_share_the_criteria_and_not_the_gaps(
    client: AsyncClient,
    index_policy: Callable[..., None],
    payer: str,
    bedrock_stub: BedrockStub,
) -> None:
    """Same payer, plan, state and CPT; different clinical context.

    Both halves are asserted deliberately. The cache hit alone would pass if the
    whole response were cached — and then the second patient would be shown the
    first patient's documentation gaps, which is a patient-safety defect rather
    than a stale-cache annoyance. The differing ``missing_criteria`` is what
    rules that out.
    """
    index_policy(payer=payer, text="Prior authorization required for knee MRI.")

    first = await client.post(
        "/policies/query",
        json=request_body(
            payer,
            clinical_context={"hpi": "Failed six weeks of conservative therapy"},
        ),
    )
    second = await client.post(
        "/policies/query",
        json=request_body(
            payer,
            clinical_context={"exam": "Neurological deficit on examination"},
        ),
    )

    first_data = first.json()["data"]
    second_data = second.json()["data"]

    # Stage 1 was cached: one model call across both queries, same criteria.
    assert bedrock_stub.calls == 1
    assert first_data["auth_criteria"] == second_data["auth_criteria"] == CRITERIA

    # Stage 2 was not: each patient's gaps are their own.
    assert first_data["missing_criteria"] != second_data["missing_criteria"]
    assert first_data["missing_criteria"] == ["Documented neurological deficit on examination"]
    assert second_data["missing_criteria"] == ["Failed six weeks of conservative therapy"]


async def test_a_patient_with_nothing_documented_is_told_so(
    client: AsyncClient,
    index_policy: Callable[..., None],
    payer: str,
    bedrock_stub: BedrockStub,
) -> None:
    """The third patient on the same cached rules, with an empty note."""
    index_policy(payer=payer, text="Prior authorization required for knee MRI.")
    await client.post("/policies/query", json=request_body(payer))

    response = await client.post("/policies/query", json=request_body(payer, clinical_context={}))

    data = response.json()["data"]
    assert data["missing_criteria"] == CRITERIA
    assert data["denial_risk"] == "high"
    assert bedrock_stub.calls == 1


# --- the fallback ----------------------------------------------------------


async def test_a_malformed_answer_is_retried_and_then_falls_back_uncached(
    client: AsyncClient,
    index_policy: Callable[..., None],
    payer: str,
    redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TASK-012's third test, end to end: retry, fall back, and cache nothing.

    A cached fallback would answer "confirm manually" for every patient on this
    plan for the next 24 hours, and would look like a healthy cache doing it.
    """
    index_policy(payer=payer, text="Prior authorization required.")
    stub = BedrockStub(["not json at all", "still not json"])
    monkeypatch.setattr(bedrock, "invoke_reasoning", stub.invoke)

    response = await client.post("/policies/query", json=request_body(payer))

    data = response.json()["data"]
    assert data["denial_risk"] == "high"
    assert data["nudge_message"] == (
        "Unable to verify authorization requirements — confirm manually"
    )
    assert stub.calls == 2
    assert await redis.get(cache_key(payer)) is None


async def test_a_good_answer_after_a_fallback_is_cached_normally(
    client: AsyncClient,
    index_policy: Callable[..., None],
    payer: str,
    redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing about the failed call is remembered, so the next one starts clean."""
    index_policy(payer=payer, text="Prior authorization required.")
    failing = BedrockStub(["not json", "still not json"])
    monkeypatch.setattr(bedrock, "invoke_reasoning", failing.invoke)
    await client.post("/policies/query", json=request_body(payer))

    working = BedrockStub([ANSWER])
    monkeypatch.setattr(bedrock, "invoke_reasoning", working.invoke)
    response = await client.post("/policies/query", json=request_body(payer))

    assert response.json()["data"]["auth_criteria"] == CRITERIA
    assert await redis.get(cache_key(payer)) is not None


# --- the audit row ---------------------------------------------------------


async def test_the_query_writes_an_audit_row(
    client: AsyncClient, index_policy: Callable[..., None], payer: str
) -> None:
    """Against the real table: "a row exists" is a different claim from "a function ran"."""
    index_policy(payer=payer, text="Prior authorization required.")
    body = request_body(payer)

    await client.post("/policies/query", json=body)

    rows = await _audit_rows(uuid.UUID(body["session_id"]))
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "QUERY_POLICY"
    assert row.service_name == "track-b-rag"
    assert str(row.actor_id) == body["provider_id"]
    assert row.resource_type == "Encounter"


async def test_the_audit_row_holds_no_clinical_detail(
    client: AsyncClient, index_policy: Callable[..., None], payer: str
) -> None:
    index_policy(payer=payer, text="Prior authorization required.")
    body = request_body(payer, clinical_context={"hpi": "fell while skiing", "mrn": "MRN-8675309"})

    await client.post("/policies/query", json=body)

    rows = await _audit_rows(uuid.UUID(body["session_id"]))
    recorded = " ".join(str(value) for value in rows[0])
    assert "skiing" not in recorded
    assert "8675309" not in recorded
    assert "73721" not in recorded


async def _audit_rows(session_id: uuid.UUID) -> list[Any]:
    """Return the audit_log rows for one session, and drop them afterwards."""
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as session:
            rows = list(await _select(session, session_id))
            await session.execute(
                sa.text("DELETE FROM audit_log WHERE session_id = :session_id"),
                {"session_id": session_id},
            )
            await session.commit()
        return rows
    finally:
        await engine.dispose()


async def _select(session: AsyncSession, session_id: uuid.UUID) -> Any:
    result = await session.execute(
        sa.text(
            "SELECT actor_id, action, resource_type, resource_id, service_name "
            "FROM audit_log WHERE session_id = :session_id"
        ),
        {"session_id": session_id},
    )
    return result.all()

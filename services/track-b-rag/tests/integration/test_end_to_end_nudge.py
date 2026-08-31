"""The whole chain, from a SMART launch to a nudge on Redis. TASK-052b.

This is the first moment two deferred acceptance criteria can actually be met:
TASK-021's stubbed policy-query seam, and TASK-024's "a published transcript
segment produces a real ``/policies/query`` call", which could not pass while
``resolve_query_parameters()`` raised for every real encounter. The payer
columns are what unblocks both, so the test that proves it belongs here.

**Nothing in the chain is stood in for except AWS Bedrock**, which has no local
mock, holds no credentials in CI, and is the one dependency CLAUDE.md already
names as unavoidably external. Everything else runs:

* ``fhir-integration`` as a **real subprocess**, reading real ``Patient``,
  ``Coverage``, ``Encounter``, ``Location`` and ``Organization`` resources from
  the real HAPI FHIR server in ``docker compose``. It runs out of process
  because several services still install a top-level ``src`` package into the
  shared virtualenv, so ``import src.main`` here would resolve to whichever
  sorts first — the shadowing CLAUDE.md warns about. A subprocess is also how
  the service actually runs.
* ``track-a-clinical`` and ``track-b-rag`` as **real HTTP servers on real
  sockets**, each on a loopback port. In process rather than as subprocesses
  only because the Bedrock stub has to be installed in the process the app runs
  in; the requests still cross a socket and run the full route stack, including
  the middleware and the ``audit_log()`` writes.
* **Real Redis, Postgres and Qdrant** from ``docker compose``, and the **real**
  :class:`TranscriptConsumer` with its default dispatch and emitter — not a
  handler called directly, which is what the fan-out test does and what this one
  exists to go beyond.

Substituting the bus, the consumer, the parameter resolution or either HTTP call
would remove precisely the seam being proven, so none of them is substituted.

**Why this needs HAPI and therefore a CI change.** The launch half of TASK-052b
reads five resource types off a FHIR server. Synthea cannot supply them — it
contains ``Coverage`` inside an ``ExplanationOfBenefit`` rather than storing it
as its own resource, and generates a single-state population — so the fixture
posts its own, the same way ``fhir-integration``'s HAPI test does. ``ci.yml``
starts ``hapi-fhir`` for this member as well as for that one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
import uvicorn
from qdrant_client import QdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import hipaa_logger
from payer_vocab import normalize_payer
from track_a_clinical.models import ClinicalNudge, Encounter
from track_b_rag import bedrock, embeddings, vector_store
from track_b_rag.config import get_settings
from track_b_rag.dedup import procedure_seen_key
from track_b_rag.transcript_consumer import TranscriptConsumer, transcription_channel

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL")
        or not os.environ.get("REDIS_URL")
        or not os.environ.get("QDRANT_HOST"),
        reason="needs a real PostgreSQL, Redis and Qdrant",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[4]
FHIR_INTEGRATION_DIR = REPO_ROOT / "services" / "fhir-integration"

HAPI_BASE_URL = os.environ.get("HAPI_FHIR_BASE_URL", "http://localhost:8080/fhir")
REQUIRE_HAPI = os.environ.get("REQUIRE_HAPI_TESTS") == "1"

VECTOR_SIZE = 1024

#: The payer spelled as a FHIR ``Coverage`` spells it. It slugs to ``bcbs-ma``,
#: a licensee this vocabulary knows — the point being that the display name the
#: EHR holds and the slug the corpus is indexed under are different strings, and
#: nothing in the chain may compare them directly.
PAYER_DISPLAY = "Blue Cross Blue Shield of Massachusetts"
PLAN_TYPE = "PPO"
SITE_STATE = "MA"
#: The patient lives somewhere else, so the run also exercises the site-of-care
#: rule rather than a case where both candidate answers agree.
PATIENT_STATE = "NH"
CPT_CODE = "73721"

POLICY_TEXT = (
    "Magnetic resonance imaging of a lower extremity joint requires prior "
    "authorization. Criteria: the member must have completed six weeks of "
    "conservative therapy, and a neurological deficit must be documented on "
    "examination."
)

#: What Sonnet would return for that policy text. Bedrock is the only substituted
#: component; this is the scripted answer, not a fixture standing in for a
#: service that could have run.
BEDROCK_ANSWER = json.dumps(
    {
        "requires_auth": True,
        "auth_criteria": [
            "Six weeks of conservative therapy",
            "Documented neurological deficit on examination",
        ],
        "step_therapy_required": False,
        "step_therapy_details": None,
    }
)

PAYER_POLICY_FIELDS = {
    "requires_auth",
    "auth_criteria",
    "step_therapy_required",
    "step_therapy_details",
    "policy_source",
}
PATIENT_SPECIFIC_FIELDS = {"missing_criteria", "denial_risk", "nudge_message"}

TRANSCRIPT_SEGMENT = (
    "The knee has been locking for a few months now. Let's go ahead and order "
    "an MRI of the right knee."
)

SETTLE_LIMIT_SECONDS = 20.0


# --- plumbing ---------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _database_url() -> str:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _qdrant() -> QdrantClient:
    return QdrantClient(
        host=os.environ["QDRANT_HOST"],
        port=int(os.environ.get("QDRANT_PORT", "6333")),
    )


async def _until(predicate: Any, limit_seconds: float = SETTLE_LIMIT_SECONDS) -> Any:
    """Wait for `predicate` to return something truthy, rather than sleeping."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_seconds
    while loop.time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError("condition was never met within the deadline")


class _Server:
    """One of this repository's FastAPI apps, on a real loopback socket."""

    def __init__(self, app: Any) -> None:
        self.port = _free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> _Server:
        self._task = asyncio.create_task(self._server.serve())
        await _until(lambda: self._server.started, limit_seconds=30.0)
        return self

    async def __aexit__(self, *_: object) -> None:
        self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


# --- the EHR side -----------------------------------------------------------


@pytest.fixture(scope="module")
def hapi() -> str:
    """The HAPI base URL, or skip — unless CI has said skipping is not allowed."""
    try:
        response = httpx.get(f"{HAPI_BASE_URL}/metadata", timeout=10.0)
        up = response.status_code == 200
    except httpx.HTTPError:
        up = False
    if up:
        return HAPI_BASE_URL
    if REQUIRE_HAPI:
        pytest.fail(
            f"REQUIRE_HAPI_TESTS=1 but no FHIR server answered at {HAPI_BASE_URL}. "
            "Start it with 'docker compose up -d hapi-fhir'."
        )
    pytest.skip(f"no HAPI FHIR server at {HAPI_BASE_URL}")


@pytest.fixture(scope="module")
def ehr_encounter_id(hapi: str) -> str:
    """Post the five resources the launch path reads, and return the encounter's id.

    Hand-posted rather than Synthea-generated, and the module docstring says why:
    a Synthea patient has no searchable ``Coverage`` at all, and every Synthea
    encounter is in the same state as its patient.
    """
    marker = uuid.uuid4().hex[:12]
    fhir_json = {"Content-Type": "application/fhir+json"}
    with httpx.Client(base_url=hapi, timeout=30.0) as client:

        def post(resource: dict[str, Any]) -> str:
            response = client.post(f"/{resource['resourceType']}", json=resource, headers=fhir_json)
            response.raise_for_status()
            return str(response.json()["id"])

        patient_id = post(
            {
                "resourceType": "Patient",
                "identifier": [{"system": "urn:medauth:test", "value": marker}],
                "name": [{"use": "official", "family": "Endtoend", "given": ["Ada"]}],
                "gender": "female",
                "birthDate": "1971-11-02",
                "address": [{"use": "home", "city": "Nashua", "state": PATIENT_STATE}],
            }
        )
        post(
            {
                "resourceType": "Coverage",
                "status": "active",
                "type": {"text": PLAN_TYPE},
                "subscriberId": f"MEM-{marker}",
                "beneficiary": {"reference": f"Patient/{patient_id}"},
                "payor": [{"display": PAYER_DISPLAY}],
            }
        )
        organization_id = post(
            {
                "resourceType": "Organization",
                "active": True,
                "name": f"Endtoend Orthopedics {marker}",
                "address": [{"city": "Leominster", "state": SITE_STATE}],
            }
        )
        location_id = post(
            {
                "resourceType": "Location",
                "status": "active",
                "name": f"Endtoend room {marker}",
                "address": {"city": "Leominster", "state": SITE_STATE},
                "managingOrganization": {"reference": f"Organization/{organization_id}"},
            }
        )
        return post(
            {
                "resourceType": "Encounter",
                "status": "in-progress",
                "subject": {"reference": f"Patient/{patient_id}"},
                "location": [{"location": {"reference": f"Location/{location_id}"}}],
                "serviceProvider": {"reference": f"Organization/{organization_id}"},
            }
        )


@pytest_asyncio.fixture
async def launch_id(hapi: str, redis: Redis) -> AsyncIterator[str]:
    """A stored SMART launch, written the way ``GET /fhir/callback`` writes one.

    The JSON is composed here rather than through ``src.smart.store`` for the
    import reason in the module docstring — that module cannot be imported from
    this service. What it must match is the record ``fhir-integration`` reads
    back, and the subprocess below is what proves it does: a shape mismatch
    makes every request 404 rather than passing quietly.
    """
    value = uuid.uuid4().hex
    await redis.set(
        f"fhir_token:{value}",
        json.dumps(
            {
                "ehr_type": "generic",
                "fhir_base_url": hapi,
                "access_token": "unused-by-hapi",
                "refresh_token": None,
                "patient_id": None,
                "encounter_id": None,
            }
        ),
        ex=600,
    )
    yield value
    await redis.delete(f"fhir_token:{value}")


@pytest.fixture(scope="module")
def fhir_integration(hapi: str) -> Iterator[str]:
    """``fhir-integration`` itself, as a subprocess, and its base URL."""
    port = _free_port()
    environment = {
        **os.environ,
        "REDIS_URL": os.environ["REDIS_URL"],
        "DATABASE_URL": os.environ["DATABASE_URL"],
    }
    process = subprocess.Popen(  # noqa: S603 - a fixed command, no shell
        [
            "uv",
            "run",
            "uvicorn",
            "src.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=FHIR_INTEGRATION_DIR,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                pytest.fail(f"fhir-integration exited before serving:\n{output}")
            try:
                if httpx.get(f"{base_url}/health", timeout=2.0).status_code in (200, 503):
                    break
            except httpx.HTTPError:
                pass
            # A blocking sleep is correct here: the fixture is synchronous and
            # nothing else in this process can make progress until the service
            # answers.
            time.sleep(0.5)
        else:
            pytest.fail("fhir-integration did not answer /health within 60s")
        yield base_url
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        process.kill()


# --- the policy corpus ------------------------------------------------------


@pytest.fixture
def collection(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A Qdrant collection of this test's own, holding one indexed policy."""
    name = f"test_e2e_{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("QDRANT_COLLECTION", name)
    get_settings.cache_clear()

    client = _qdrant()
    vector_store.ensure_collection(client, name, VECTOR_SIZE)
    vector_store.ensure_payload_indexes(client, name)
    vector_store.upsert_points(
        client,
        name,
        vector_store.build_points(
            policy_id=f"POL-{uuid.uuid4().hex[:8]}",
            # Ingestion indexes under the slug. Indexing the display name here
            # would make the retrieval test a filter production never uses.
            payer=normalize_payer(PAYER_DISPLAY),
            plan_type=PLAN_TYPE,
            state=SITE_STATE,
            chunks=[POLICY_TEXT],
            vectors=[[0.1] * VECTOR_SIZE],
        ),
    )
    try:
        yield name
    finally:
        client.delete_collection(name)
        client.close()
        get_settings.cache_clear()


@pytest.fixture
def stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic vectors of the right width, in place of 1.3 GB of weights.

    The same substitution every other integration module in this service makes.
    It is not one of the chain's seams: retrieval, the filter and the store are
    all still real, and the vector is what the corpus was indexed with.
    """
    monkeypatch.setattr(embeddings, "embed_query", lambda text: [0.1] * VECTOR_SIZE)
    monkeypatch.setattr(
        embeddings, "embed_documents", lambda texts: [[0.1] * VECTOR_SIZE for _ in texts]
    )


@pytest.fixture
def bedrock_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one permitted substitution. See the module docstring."""

    async def invoke(prompt: str) -> str:
        return BEDROCK_ANSWER

    monkeypatch.setattr(bedrock, "invoke_reasoning", invoke)


# --- the running services ---------------------------------------------------


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(os.environ["REDIS_URL"])
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def audit_pool() -> AsyncIterator[None]:
    """Close hipaa-logger's pool after each test — it binds to one event loop."""
    yield
    await hipaa_logger.close_pool()


@pytest_asyncio.fixture
async def track_b_rag_server(
    collection: str,
    stub_embedder: None,
    bedrock_stub: None,
    audit_pool: None,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Server]:
    """``track-b-rag`` on a loopback socket, wired to the real stores.

    In process so the Bedrock stub above applies to the app; on a real socket so
    the consumer's ``POST /policies/query`` is a real HTTP request through the
    real route, and its ``audit_log()`` row is written the way production writes
    it.
    """
    from track_b_rag.main import create_app

    async with _Server(create_app()) as server:
        monkeypatch.setenv("POLICY_QUERY_BASE_URL", server.base_url)
        get_settings.cache_clear()
        yield server
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def track_a_clinical_server(
    fhir_integration: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Server]:
    """``track-a-clinical`` on a loopback socket, pointed at the real launch service."""
    monkeypatch.setenv("FHIR_INTEGRATION_URL", fhir_integration)
    monkeypatch.setenv("JWT_SIGNING_KEY", "e2e-signing-key-of-at-least-32-bytes-length")
    monkeypatch.setenv("REDIS_URL", os.environ["REDIS_URL"])

    from track_a_clinical.config import get_settings as track_a_settings
    from track_a_clinical.main import create_app

    track_a_settings.cache_clear()
    async with _Server(create_app()) as server:
        yield server
    track_a_settings.cache_clear()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[Any]]:
    engine = create_async_engine(_database_url())
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def consumer(redis: Redis) -> AsyncIterator[TranscriptConsumer]:
    """The real consumer with its real dispatch and its real emitter.

    Started **before** the session, so it is subscribed to ``sessions:started``
    when ``POST /sessions/start`` publishes — which is the ordering the endpoint
    guarantees and this test relies on rather than works around.
    """
    running = TranscriptConsumer(redis)
    running.start()
    await _until(running.is_healthy, limit_seconds=10.0)
    yield running
    await running.stop()


# --- the run ----------------------------------------------------------------


async def test_a_transcript_segment_produces_a_real_policy_query_and_a_nudge(
    redis: Redis,
    db: async_sessionmaker[Any],
    consumer: TranscriptConsumer,
    track_a_clinical_server: _Server,
    track_b_rag_server: _Server,
    ehr_encounter_id: str,
    launch_id: str,
) -> None:
    """TASK-021's stubbed seam and TASK-024's deferred criterion, both for real."""
    nudges = redis.pubsub()
    async with httpx.AsyncClient(timeout=30.0) as http:
        started = await http.post(
            f"{track_a_clinical_server.base_url}/sessions/start",
            json={
                "patient_id": "e2e-patient",
                "provider_id": str(uuid.uuid4()),
                "ehr_encounter_id": ehr_encounter_id,
                "launch_id": launch_id,
            },
        )
    assert started.status_code == 201, started.text
    session_id = uuid.UUID(started.json()["data"]["session_id"])

    # The launch half: three columns read off a real FHIR server, on a real row.
    async with db() as session:
        encounter = await session.scalar(
            sa.select(Encounter).where(Encounter.session_id == session_id)
        )
        assert encounter is not None
        assert encounter.launch_id == launch_id
        assert encounter.insurance_payer == PAYER_DISPLAY
        assert encounter.insurance_plan_type == PLAN_TYPE
        # The site of care, not the patient's NH residence.
        assert encounter.state == SITE_STATE

    await nudges.subscribe(f"nudges:{session_id}")
    try:
        await redis.publish(
            transcription_channel(session_id),
            json.dumps(
                {
                    "session_id": str(session_id),
                    "result_id": str(uuid.uuid4()),
                    "text": TRANSCRIPT_SEGMENT,
                    "is_partial": False,
                    "start_time": 12.0,
                    "end_time": 17.5,
                }
            ),
        )

        message = await _until(
            lambda: nudges.get_message(ignore_subscribe_messages=True, timeout=0.1)
        )
    finally:
        await nudges.aclose()

    payload = json.loads(message["data"])
    assert payload["type"] == "PAYER_RULE_ALERT"
    assert payload["cpt_code"] == CPT_CODE
    assert payload["missing_criteria"]
    assert payload["message"]

    # The dedup claim, made by the real consumer against the real broker.
    claimed = await redis.smembers(procedure_seen_key(session_id))
    assert b"cpt:73721" in claimed

    # The cache key carries the canonical slug, not the display name the EHR
    # holds. This is the first run where a real payer value reaches it, and the
    # failure it guards against is the one TASK-016/TASK-017 fixed once already.
    key = f"rag:{normalize_payer(PAYER_DISPLAY)}:{PLAN_TYPE}:{SITE_STATE}:{CPT_CODE}"
    cached = await redis.get(key)
    assert cached is not None, f"no payer-policy cache entry at {key}"
    assert not await redis.exists(f"rag:{PAYER_DISPLAY}:{PLAN_TYPE}:{SITE_STATE}:{CPT_CODE}")

    # And it carries only the payer's half. The patient-specific fields are
    # recomputed per call; cached here they would serve the next patient on this
    # plan the gaps computed for this one.
    document = json.loads(cached)
    assert set(document) <= PAYER_POLICY_FIELDS
    assert not set(document) & PATIENT_SPECIFIC_FIELDS

    async with db() as session:
        rows = (
            (
                await session.execute(
                    sa.select(ClinicalNudge).where(ClinicalNudge.encounter_id == encounter.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert str(rows[0].id) == payload["nudge_id"]

    async with db() as session:
        await session.execute(sa.delete(ClinicalNudge).where(ClinicalNudge.id == rows[0].id))
        await session.execute(sa.delete(Encounter).where(Encounter.id == encounter.id))
        await session.commit()
    await redis.delete(procedure_seen_key(session_id), key)

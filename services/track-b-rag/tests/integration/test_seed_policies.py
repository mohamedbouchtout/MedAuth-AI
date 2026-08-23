"""``scripts/seed-policies.py`` — the commercial payer dev corpus.

Tested from track-b-rag rather than from a suite of its own, following the
precedent that ``scripts/seed-test-encounters.py`` is tested from
``services/track-a-clinical/tests/integration/``. The script's whole job is to
call this service's ingest endpoint, so this is where its behaviour is
observable.

Every test here runs against fixtures. The live checks that actually fetch from
Aetna and BCBSMA are gated on ``RUN_PAYER_LIVE_TESTS`` at the bottom of this
module, and the nightly workflow is what turns that gate on — see CLAUDE.md on
why a gate without a scheduled run is a deleted test.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "seed-policies.py"


def _load_script() -> ModuleType:
    """Import the seed script by path — its filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location("seed_policies", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_policies"] = module
    spec.loader.exec_module(module)
    return module


seed_policies = _load_script()


class _StubFetcher:
    """Stands in for PoliteClient, recording what was asked for."""

    def __init__(self, bodies: dict[str, bytes] | None = None, error: Exception | None = None):
        self._bodies = bodies or {}
        self._error = error
        self.requested: list[str] = []

    async def get(self, url: str) -> bytes:
        self.requested.append(url)
        if self._error is not None:
            raise self._error
        return self._bodies.get(url, b"policy text")

    async def __aenter__(self) -> _StubFetcher:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _ingest_client(
    responses: list[httpx.Response] | None = None,
    recorder: list[httpx.Request] | None = None,
) -> httpx.AsyncClient:
    """An httpx client whose transport answers with canned ingest responses."""
    queue = list(responses or [])

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        if queue:
            return queue.pop(0)
        return httpx.Response(
            200,
            json={
                "data": {"policy_id": "x", "status": "created", "chunks_indexed": 3},
                "error": None,
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestSeedList:
    """The curated constant itself. A list nobody checks stops being curated."""

    def test_every_policy_id_is_unique(self) -> None:
        """Two entries sharing an id would silently overwrite each other in Qdrant."""
        ids = [p.policy_id for p in seed_policies.SEED_POLICIES]
        assert len(ids) == len(set(ids))

    def test_every_source_url_is_unique(self) -> None:
        ids = [p.source_url for p in seed_policies.SEED_POLICIES]
        assert len(ids) == len(set(ids))

    def test_the_list_is_not_empty(self) -> None:
        """An empty corpus would make every downstream query return nothing."""
        assert seed_policies.SEED_POLICIES

    @pytest.mark.parametrize("policy", seed_policies.SEED_POLICIES, ids=lambda p: p.policy_id)
    def test_content_type_is_one_ingest_accepts(self, policy: Any) -> None:
        assert policy.content_type in {"application/pdf", "text/html"}

    @pytest.mark.parametrize("policy", seed_policies.SEED_POLICIES, ids=lambda p: p.policy_id)
    def test_source_url_is_https(self, policy: Any) -> None:
        """Plaintext HTTP is out, per the TLS-everywhere constraint."""
        assert policy.source_url.startswith("https://")

    @pytest.mark.parametrize("policy", seed_policies.SEED_POLICIES, ids=lambda p: p.policy_id)
    def test_state_is_a_usps_code_or_absent(self, policy: Any) -> None:
        assert policy.state is None or (len(policy.state) == 2 and policy.state.isupper())

    def test_both_content_types_are_represented(self) -> None:
        """Aetna is HTML and BCBSMA is PDF, so this corpus exercises both paths.

        If this ever fails it means one payer dropped out of the list, and the
        HTML-vs-PDF coverage this suite claims to give would be silently gone.
        """
        assert {p.content_type for p in seed_policies.SEED_POLICIES} == {
            "application/pdf",
            "text/html",
        }


class TestPayerSlugs:
    """The bug packages/payer-vocab exists to prevent, checked at the seed side."""

    @pytest.mark.parametrize("policy", seed_policies.SEED_POLICIES, ids=lambda p: p.policy_id)
    def test_every_payer_resolves_to_a_curated_slug(self, policy: Any) -> None:
        """An uncurated slug here means the corpus indexes where no query looks."""
        from payer_vocab import is_known_payer, normalize_payer

        assert is_known_payer(normalize_payer(policy.payer))

    def test_bcbsma_seeds_under_the_licensee_slug(self) -> None:
        """Never the generic bucket: licensees publish their own criteria."""
        bcbs = [p for p in seed_policies.SEED_POLICIES if "bluecrossma.org" in p.source_url]
        assert bcbs, "expected BCBSMA documents in the corpus"
        for policy in bcbs:
            assert seed_policies.normalize_payer(policy.payer) == "bcbs-ma"

    def test_form_fields_send_the_slug_not_the_display_name(self) -> None:
        """Qdrant matches this string exactly, so a display name retrieves nothing."""
        policy = seed_policies.SeedPolicy(
            policy_id="p1",
            payer="Blue Cross Blue Shield of Massachusetts",
            title="t",
            source_url="https://example.test/p.pdf",
            content_type="application/pdf",
            state="MA",
        )
        assert seed_policies._form_fields(policy)["payer"] == "bcbs-ma"

    def test_unknown_payer_still_seeds_but_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """A payer we have not curated is not an error — but it must be visible."""
        policy = seed_policies.SeedPolicy(
            policy_id="p1",
            payer="Sierra Valley Regional Health Plan",
            title="t",
            source_url="https://example.test/p.pdf",
            content_type="application/pdf",
        )
        with caplog.at_level("WARNING"):
            fields = seed_policies._form_fields(policy)
        assert fields["payer"] == "sierra-valley-regional-health-plan"
        assert "unrecognised payer slug" in caplog.text

    def test_state_is_omitted_for_a_national_policy(self) -> None:
        """Omitted rather than empty: ingest treats a blank state as None anyway,
        but sending the field at all would claim a jurisdiction the payer did not."""
        policy = seed_policies.SeedPolicy(
            policy_id="p1",
            payer="Aetna",
            title="t",
            source_url="https://example.test/p.html",
            content_type="text/html",
        )
        assert "state" not in seed_policies._form_fields(policy)


class TestSeedOne:
    """One document, fetched and uploaded."""

    @pytest.mark.parametrize(
        ("content_type", "suffix"),
        [("application/pdf", "pdf"), ("text/html", "html")],
    )
    @pytest.mark.asyncio
    async def test_upload_declares_the_content_type(self, content_type: str, suffix: str) -> None:
        """Both ingest paths are exercised from this caller, not just the PDF one.

        Aetna is why the HTML path is in scope here: with CMS owned by
        policy-scraper it would be easy to assume this script is PDF-only, and
        it is not.
        """
        policy = seed_policies.SeedPolicy(
            policy_id="p1",
            payer="Aetna",
            title="t",
            source_url="https://example.test/p",
            content_type=content_type,
        )
        requests: list[httpx.Request] = []
        async with _ingest_client(recorder=requests) as client:
            status = await seed_policies.seed_one(
                _StubFetcher(), client, base_url="http://rag.test", policy=policy
            )
        assert status == "created"
        body = requests[0].content.decode("latin-1")
        assert f'name="content_type"\r\n\r\n{content_type}' in body
        assert f"p1.{suffix}" in body

    @pytest.mark.asyncio
    async def test_posts_to_the_ingest_route(self) -> None:
        requests: list[httpx.Request] = []
        async with _ingest_client(recorder=requests) as client:
            await seed_policies.seed_one(
                _StubFetcher(),
                client,
                base_url="http://rag.test",
                policy=seed_policies.SEED_POLICIES[0],
            )
        assert str(requests[0].url) == "http://rag.test/policies/ingest"

    @pytest.mark.asyncio
    async def test_returns_the_status_ingest_reported(self) -> None:
        """created / updated / unchanged all come straight from the endpoint —
        this script does not form its own opinion about what happened."""
        response = httpx.Response(
            200,
            json={
                "data": {"policy_id": "p1", "status": "unchanged", "chunks_indexed": 0},
                "error": None,
            },
        )
        async with _ingest_client([response]) as client:
            status = await seed_policies.seed_one(
                _StubFetcher(),
                client,
                base_url="http://rag.test",
                policy=seed_policies.SEED_POLICIES[0],
            )
        assert status == "unchanged"

    @pytest.mark.asyncio
    async def test_fetch_failure_raises(self) -> None:
        fetcher = _StubFetcher(error=httpx.ConnectError("refused"))
        async with _ingest_client() as client:
            with pytest.raises(seed_policies.SeedFailed, match="Could not fetch"):
                await seed_policies.seed_one(
                    fetcher,
                    client,
                    base_url="http://rag.test",
                    policy=seed_policies.SEED_POLICIES[0],
                )

    @pytest.mark.asyncio
    async def test_robots_disallowed_raises_rather_than_skipping(self) -> None:
        """Loud, for TASK-013's reason: a run that fetched nothing because a rule
        changed looks exactly like a run with nothing to fetch."""
        from policy_scraper.fetch import RobotsDisallowed

        fetcher = _StubFetcher(error=RobotsDisallowed("no"))
        async with _ingest_client() as client:
            with pytest.raises(seed_policies.SeedFailed):
                await seed_policies.seed_one(
                    fetcher,
                    client,
                    base_url="http://rag.test",
                    policy=seed_policies.SEED_POLICIES[0],
                )

    @pytest.mark.asyncio
    async def test_empty_document_is_refused_before_upload(self) -> None:
        """An empty body would hash and index to a vector that says nothing, and
        every later run would then report it as 'unchanged'."""
        policy = seed_policies.SEED_POLICIES[0]
        fetcher = _StubFetcher({policy.source_url: b"   \n  "})
        requests: list[httpx.Request] = []
        async with _ingest_client(recorder=requests) as client:
            with pytest.raises(seed_policies.SeedFailed, match="empty document"):
                await seed_policies.seed_one(
                    fetcher, client, base_url="http://rag.test", policy=policy
                )
        assert requests == []

    @pytest.mark.asyncio
    async def test_error_status_raises(self) -> None:
        async with _ingest_client([httpx.Response(422, text="bad form")]) as client:
            with pytest.raises(seed_policies.SeedFailed, match="HTTP 422"):
                await seed_policies.seed_one(
                    _StubFetcher(),
                    client,
                    base_url="http://rag.test",
                    policy=seed_policies.SEED_POLICIES[0],
                )

    @pytest.mark.asyncio
    async def test_unrecognised_body_raises(self) -> None:
        """A 200 with the wrong shape is not success — counting it as ingested
        would report a corpus that is not there."""
        async with _ingest_client(
            [httpx.Response(200, json={"data": None, "error": None})]
        ) as client:
            with pytest.raises(seed_policies.SeedFailed, match="unrecognised body"):
                await seed_policies.seed_one(
                    _StubFetcher(),
                    client,
                    base_url="http://rag.test",
                    policy=seed_policies.SEED_POLICIES[0],
                )


class TestSeedRun:
    """The whole run, and how it reports partial failure."""

    @pytest.mark.asyncio
    async def test_one_failure_does_not_stop_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A payer reorganising one URL should not cost the whole corpus."""
        seen: list[str] = []

        async def fake_seed_one(_f: object, _c: object, *, base_url: str, policy: Any) -> str:
            seen.append(policy.policy_id)
            if policy.policy_id == "b":
                raise seed_policies.SeedFailed("nope")
            return "created"

        monkeypatch.setattr(seed_policies, "seed_one", fake_seed_one)
        policies = tuple(
            seed_policies.SeedPolicy(
                policy_id=pid,
                payer="Aetna",
                title="t",
                source_url=f"https://example.test/{pid}",
                content_type="text/html",
            )
            for pid in ("a", "b", "c")
        )
        failures = await seed_policies.seed("http://rag.test", policies)
        assert failures == 1
        assert seen == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_a_clean_run_reports_no_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_seed_one(*_a: object, **_k: object) -> str:
            return "created"

        monkeypatch.setattr(seed_policies, "seed_one", fake_seed_one)
        assert await seed_policies.seed("http://rag.test", seed_policies.SEED_POLICIES) == 0


class TestMain:
    @staticmethod
    def _run_returning(failures: int) -> Any:
        """Replace asyncio.run with a stub that closes the coroutine it is handed.

        Discarding it instead would leave it un-awaited, which pytest reports as
        a RuntimeWarning on an otherwise clean suite.
        """

        def fake_run(coro: Any) -> int:
            coro.close()
            return failures

        return fake_run

    @pytest.mark.parametrize(("failures", "exit_code"), [(2, 1), (1, 1), (0, 0)])
    def test_exit_code_reflects_whether_anything_failed(
        self, monkeypatch: pytest.MonkeyPatch, failures: int, exit_code: int
    ) -> None:
        """Kubernetes and a human both read the exit code, not the log."""
        monkeypatch.setattr(seed_policies.sys, "argv", ["seed-policies.py"])
        monkeypatch.setattr(seed_policies.asyncio, "run", self._run_returning(failures))
        assert seed_policies.main() == exit_code

    def test_base_url_comes_from_the_environment_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        monkeypatch.setenv("TRACK_B_RAG_URL", "http://rag.internal:8002")
        monkeypatch.setattr(seed_policies.sys, "argv", ["seed-policies.py"])

        async def fake_seed(base_url: str, *_a: object) -> int:
            seen.append(base_url)
            return 0

        monkeypatch.setattr(seed_policies, "seed", fake_seed)
        assert seed_policies.main() == 0
        assert seen == ["http://rag.internal:8002"]


RUN_LIVE = os.environ.get("RUN_PAYER_LIVE_TESTS") == "1"
live = pytest.mark.skipif(
    not RUN_LIVE,
    reason="Set RUN_PAYER_LIVE_TESTS=1 to fetch from Aetna and BCBSMA. "
    "The nightly workflow does this on a schedule.",
)


@live
class TestAgainstLivePayers:
    """Fetches the real documents. Gated off by default, run nightly.

    A failure here is a real signal that a payer moved or withdrew a document,
    not a flake. Fix the URL in ``SEED_POLICIES``; do not relax the assertion.
    """

    @pytest.mark.parametrize("policy", seed_policies.SEED_POLICIES, ids=lambda p: p.policy_id)
    @pytest.mark.asyncio
    async def test_document_still_resolves_to_its_declared_type(self, policy: Any) -> None:
        async with seed_policies.PoliteClient(
            user_agent=seed_policies.USER_AGENT,
            delay_seconds=seed_policies.DELAY_SECONDS,
            timeout_seconds=seed_policies.TIMEOUT_SECONDS,
        ) as fetcher:
            body = await fetcher.get(policy.source_url)

        stripped = body.strip()
        assert stripped, f"{policy.policy_id} came back empty"
        if policy.content_type == "application/pdf":
            assert stripped.startswith(b"%PDF-"), f"{policy.policy_id} is no longer a PDF"
        else:
            # Aetna's CPB pages open with roughly two kilobytes of template
            # whitespace before the first tag, so this looks at the stripped
            # body rather than at a fixed prefix of the raw bytes.
            assert stripped.startswith(b"<"), f"{policy.policy_id} is no longer HTML"

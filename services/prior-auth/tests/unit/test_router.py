"""The routing model: which path a request takes, and why. TASK-061.

Four properties here are rules rather than implementation details, and each is
something the task would be wrong without:

* **One question decides the path — does this payer support PAS.** Which
  transport that becomes is the adapter's business, one service over.
* **The payer name is normalised before the question is asked.** Comparing a
  display name against a slug set answers "no automated path" for every payer
  spelled the way a FHIR ``Coverage`` spells it, silently and indistinguishably
  from a payer genuinely outside the mandate.
* **A payer with no automated path is handed to a person, never transmitted.**
  It is the ordinary case, not a failure, and it must never look submitted.
* **"We could not ask" is not "there is nothing to ask".** An unreachable
  submitter raises; it does not quietly take a submittable request off the
  automated path.
"""

from __future__ import annotations

from typing import Any

import pytest

from prior_auth import router, submission_client
from prior_auth.config import get_settings
from track_a_clinical.models import PRIOR_AUTH_STATUS_MANUAL_REQUIRED

REQUEST_ID = "0b1d9a2e-6f43-4c1a-9d21-4e8b7f0c5a33"
LAUNCH_ID = "launch-2f9c"


def facts(
    *,
    payer_name: str | None = "Medicare Advantage",
    launch_id: str | None = LAUNCH_ID,
    status: str = "pending",
) -> submission_client.RoutingFacts:
    """The owning service's answer about one request."""
    return submission_client.RoutingFacts(
        request_id=REQUEST_ID,
        status=status,
        payer_name=payer_name,
        launch_id=launch_id,
        submittable=True,
    )


class FakeSession:
    """Records the manual-case write without a database.

    ``execute`` keeps the statement rather than the values it was given, so a
    test can assert on what was actually set — a fake that recorded its own
    constants could not fail on code that wrote something else.
    """

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)

    async def commit(self) -> None:
        self.commits += 1

    def written_values(self) -> dict[str, Any]:
        assert len(self.statements) == 1, "expected exactly one write"
        return dict(self.statements[0].compile().params)


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def submitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture calls to the submitter, answering with an adjudicated request."""
    calls: list[dict[str, Any]] = []

    async def fake_submit(request_id: str, **kwargs: Any) -> submission_client.SubmissionResult:
        calls.append({"request_id": request_id, **kwargs})
        return submission_client.SubmissionResult(
            request_id=request_id,
            outcome="complete",
            submission_method="fhir-pas",
            payer_reference_number="AUTH-88213",
        )

    monkeypatch.setattr(submission_client, "submit_through_fhir_integration", fake_submit)
    return calls


class TestChoosingAPath:
    """The one question, and how the payer name reaches it."""

    def test_capability_is_resolved_through_the_shared_vocabulary(self) -> None:
        """One definition of the CMS-0057-F set, read by both consumers.

        ``track_b_rag.crd`` asks the same question mid-encounter. It held the
        set privately until this task became its second consumer; a literal
        copied here instead would be the two-spellings failure the vocabulary
        exists to prevent, one level up.
        """
        from payer_vocab import supports_prior_auth_api

        assert router.supports_prior_auth_api is supports_prior_auth_api

    def test_a_mandated_payer_has_an_automated_path(self) -> None:
        slug, reason = router.choose_path("Medicare Advantage")

        assert slug == "medicare-advantage"
        assert reason is None

    def test_a_commercial_payer_does_not(self) -> None:
        """The bulk of what private practices see. Ordinary, not a failure."""
        slug, reason = router.choose_path("Aetna")

        assert slug == "aetna"
        assert reason == router.REASON_PAYER_NOT_SUPPORTED

    def test_a_display_name_is_normalised_before_the_question(self) -> None:
        """The failure packages/payer-vocab exists to prevent, asserted here.

        A FHIR ``Coverage`` spells the payer however the EHR spells it. Matching
        that raw string against a slug set would route every one of these to a
        person while looking exactly like a settled answer about the payer.

        Every spelling below is one the vocabulary already curates — including
        the carrier-branded forms, which is the case a bare slug comparison
        would most obviously miss. Spellings it does not know are the next test,
        and they are deliberately not added here: that table is extended from
        observed data, never from plausible-looking strings.
        """
        for spelling in (
            "MEDICARE ADVANTAGE",
            "Medicare Advantage",
            "Medicare Part C",
            "Humana Medicare Advantage",
        ):
            slug, reason = router.choose_path(spelling)
            assert slug == "medicare-advantage", spelling
            assert reason is None, spelling

    def test_an_unrecognised_payer_is_still_routed(self, caplog: pytest.LogCaptureFixture) -> None:
        """Routed on the same rule, and the mismatch is visible in the trace.

        Not an error and not a refusal — the same arrangement as an unknown
        payer at query time, so "the name did not line up" never looks like a
        settled answer.
        """
        with caplog.at_level("WARNING"):
            slug, reason = router.choose_path("Some Regional Health Plan")

        assert slug == "some-regional-health-plan"
        assert reason == router.REASON_PAYER_NOT_SUPPORTED
        assert "unrecognised payer" in caplog.text

    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_a_missing_payer_name_needs_a_person(self, missing: str | None) -> None:
        """Reachable by design: TASK-060 assembles without a payer rather than
        discarding an encounter's findings."""
        slug, reason = router.choose_path(missing)

        assert slug is None
        assert reason == router.REASON_NO_PAYER_RECORDED


class TestRoutingASubmission:
    """What the router does with the answer."""

    async def test_a_supported_payer_is_submitted_through_fhir_integration(
        self, session: FakeSession, submitted: list[dict[str, Any]]
    ) -> None:
        decision = await router.route_submission(
            session,  # type: ignore[arg-type]
            request_id=REQUEST_ID,
            facts=facts(),
        )

        assert decision.outcome is router.RoutingOutcome.SUBMITTED
        assert decision.payer_slug == "medicare-advantage"
        assert decision.result is not None
        assert decision.result.submission_method == "fhir-pas"
        assert len(submitted) == 1
        assert submitted[0]["request_id"] == REQUEST_ID
        assert submitted[0]["launch_id"] == LAUNCH_ID
        # Nothing was recorded here: the submitter's own callback is the only
        # writer of the outcome columns.
        assert session.statements == []

    async def test_an_unsupported_payer_transmits_nothing(
        self, session: FakeSession, submitted: list[dict[str, Any]]
    ) -> None:
        decision = await router.route_submission(
            session,  # type: ignore[arg-type]
            request_id=REQUEST_ID,
            facts=facts(payer_name="Aetna"),
        )

        assert decision.outcome is router.RoutingOutcome.MANUAL_REQUIRED
        assert decision.reason == router.REASON_PAYER_NOT_SUPPORTED
        assert decision.result is None
        assert submitted == []

    async def test_the_manual_case_writes_only_the_status(
        self, session: FakeSession, submitted: list[dict[str, Any]]
    ) -> None:
        """The single-writer rule, guarded.

        ``submission_method``, ``payer_outcome`` and ``payer_reference_number``
        are written only by the owning service's record route, after a payer has
        actually answered. Nothing transmitted anything here, so writing any of
        them would be recording a submission that did not happen.
        """
        await router.route_submission(
            session,  # type: ignore[arg-type]
            request_id=REQUEST_ID,
            facts=facts(payer_name="Aetna"),
        )

        values = session.written_values()
        assert PRIOR_AUTH_STATUS_MANUAL_REQUIRED in values.values()
        assert not {
            "submission_method",
            "payer_outcome",
            "payer_reference_number",
            "submitted_at",
        } & set(values)
        assert session.commits == 1

    async def test_an_unconfigured_path_is_the_manual_case(
        self, session: FakeSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The payer takes PAS; this EHR delivers by CoverMyMeds and nothing
        configured it. Same outcome, one layer down — and never a retry, because
        no amount of retrying configures a credential."""

        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise submission_client.SubmissionRefused(
                submission_client.ERROR_CODE_PATH_NOT_CONFIGURED,
                status_code=422,
            )

        monkeypatch.setattr(submission_client, "submit_through_fhir_integration", refuse)

        decision = await router.route_submission(
            session,  # type: ignore[arg-type]
            request_id=REQUEST_ID,
            facts=facts(),
        )

        assert decision.outcome is router.RoutingOutcome.MANUAL_REQUIRED
        assert decision.reason == router.REASON_PATH_NOT_CONFIGURED

    async def test_another_refusal_is_raised_rather_than_recorded_as_manual(
        self, session: FakeSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A request the submitter could not make conformant is not "no path"."""

        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise submission_client.SubmissionRefused("PRIOR_AUTH_NOT_SUBMITTABLE", status_code=422)

        monkeypatch.setattr(submission_client, "submit_through_fhir_integration", refuse)

        with pytest.raises(submission_client.SubmissionRefused):
            await router.route_submission(
                session,  # type: ignore[arg-type]
                request_id=REQUEST_ID,
                facts=facts(),
            )
        assert session.statements == []

    async def test_an_unreachable_submitter_does_not_become_a_manual_outcome(
        self, session: FakeSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "We could not ask" is not "there is nothing to ask".

        Recording the latter would take a submittable request off the automated
        path because of a network blip, and nothing would ever put it back.
        """

        async def unavailable(*_args: Any, **_kwargs: Any) -> None:
            raise submission_client.SubmissionUnavailable("unreachable")

        monkeypatch.setattr(submission_client, "submit_through_fhir_integration", unavailable)

        with pytest.raises(submission_client.SubmissionUnavailable):
            await router.route_submission(
                session,  # type: ignore[arg-type]
                request_id=REQUEST_ID,
                facts=facts(),
            )
        assert session.statements == []

    async def test_a_missing_launch_is_logged_as_a_broken_invariant(
        self,
        session: FakeSession,
        submitted: list[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """TASK-060 does not assemble a bundle for an encounter with no launch.

        So this is not an ordinary branch — it is that guarantee having broken.
        Recorded as manual rather than raised, because the request exists and a
        person can still file it, but logged at ERROR so it is not silent.
        """
        with caplog.at_level("ERROR"):
            decision = await router.route_submission(
                session,  # type: ignore[arg-type]
                request_id=REQUEST_ID,
                facts=facts(launch_id=None),
            )

        assert decision.outcome is router.RoutingOutcome.MANUAL_REQUIRED
        assert decision.reason == router.REASON_NO_LAUNCH
        assert submitted == []
        assert "no launch_id" in caplog.text

    async def test_an_unsupported_payer_with_no_launch_reports_the_payer(
        self, session: FakeSession
    ) -> None:
        """The payer question is asked first, so the reason names the real cause.

        Both facts are true of such a request; reporting the launch would send
        someone looking for an EHR linkage problem when the payer simply has no
        API.
        """
        decision = await router.route_submission(
            session,  # type: ignore[arg-type]
            request_id=REQUEST_ID,
            facts=facts(payer_name="Aetna", launch_id=None),
        )

        assert decision.reason == router.REASON_PAYER_NOT_SUPPORTED


class TestTheSubmissionTimeoutIsConfigured:
    async def test_the_configured_budget_reaches_the_submission(
        self, session: FakeSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the behaviour follows the setting rather than a literal.

        The value bounds a round trip to a payer we do not control, and a
        literal would be unchangeable without a release. Asserted on what the
        call actually received, not on the setting alone — reading the setting
        somewhere and passing a constant here would still pass that.
        """
        get_settings.cache_clear()
        monkeypatch.setenv("SUBMISSION_TIMEOUT_SECONDS", "42.5")
        seen: list[float | None] = []

        async def record(_request_id: str, **kwargs: Any) -> submission_client.SubmissionResult:
            seen.append(kwargs.get("timeout_seconds"))
            return submission_client.SubmissionResult(
                request_id=REQUEST_ID,
                outcome="queued",
                submission_method="fhir-pas",
            )

        monkeypatch.setattr(submission_client, "submit_through_fhir_integration", record)
        try:
            await router.route_submission(
                session,  # type: ignore[arg-type]
                request_id=REQUEST_ID,
                facts=facts(),
            )
        finally:
            get_settings.cache_clear()

        assert seen == [42.5]

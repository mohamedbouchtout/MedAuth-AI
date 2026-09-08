"""Choosing how a prior authorization leaves this system. TASK-061.

**One question is asked here: does this payer support the Da Vinci PAS API?**
Everything else follows from the answer, and the shape of that is the substance
of this task rather than an implementation detail.

* **Yes** — call ``fhir-integration``'s ``POST /fhir/prior-auth`` and relay what
  came back. Which *transport* that becomes is deliberately invisible from here:
  the adapter layer decides between the payer's FHIR endpoint and an EHR's
  CoverMyMeds fallback, and it has decided that since TASK-054.
* **No** — record that the request needs a person, and transmit nothing. Most
  commercial employer-sponsored plans are outside the CMS-0057-F mandate, which
  is the same population CLAUDE.md's two-tier policy lookup already says takes
  the RAG path alone. **This is the ordinary case, not a failure.**

**This service never speaks CoverMyMeds.** An earlier draft of TASK-061 had it
calling that API directly when a payer had no PAS support, which was written
before TASK-054 existed. That task shipped CoverMyMeds inside
``fhir-integration`` as ``AthenaAdapter``'s override, with the vendor credential
in that service's settings. Building the old wording would have put a second
client and a second copy of a credential in a second service.

The two tasks route on different axes and that is what made them look
compatible: TASK-054 routes by *EHR* (Athenahealth publishes no FHIR PAS), this
one by *payer* (this payer publishes no PAS endpoint at all). They compose in one
direction only — ask about the payer, and let the adapter answer for the EHR.

**Nothing here records a submission result.** ``POST /fhir/prior-auth`` already
calls back into ``track-a-clinical``, which is the only writer of
``submission_method``, ``payer_outcome`` and ``payer_reference_number`` and
enforces write-once on them. The one thing this module writes is its own routing
outcome, in the manual case, which nothing else knows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from payer_vocab import is_known_payer, normalize_payer, supports_prior_auth_api
from prior_auth import submission_client
from prior_auth.config import get_settings
from track_a_clinical.models import PRIOR_AUTH_STATUS_MANUAL_REQUIRED, PriorAuthRequest

logger = logging.getLogger(__name__)


class RoutingOutcome(StrEnum):
    """What happened to a request the router was asked to submit.

    A closed vocabulary rather than a bare string, on the same terms as
    ``SubmissionMethod`` and ``EHRType``: it is compared by equality, it is
    rendered by a dashboard, and the type is what stops two spellings of one
    outcome existing.
    """

    #: Transmitted, and the payer answered. What it *said* is in the result's
    #: ``outcome`` — this only says the submission happened.
    SUBMITTED = "submitted"

    #: No automated path exists for this payer, so a person submits it. Recorded
    #: on the row and never transmitted anywhere.
    MANUAL_REQUIRED = "manual-submission-required"


@dataclass(frozen=True)
class RoutingDecision:
    """What the router did, and what it learned doing it.

    Attributes:
        outcome: Which of the two things happened.
        payer_slug: The canonical slug the capability question was asked about.
            Kept so a surprising decision can be traced to the name it was
            derived from, which is the failure ``packages/payer-vocab`` exists to
            make visible.
        result: The payer's answer, when there was a submission. None otherwise.
        reason: Why a manual submission is needed, when one is. None otherwise.
    """

    outcome: RoutingOutcome
    payer_slug: str | None
    result: submission_client.SubmissionResult | None = None
    reason: str | None = None


#: Why a request needs a person. Fixed labels rather than prose, so a dashboard
#: can branch on them and a log line does not carry a sentence somebody has to
#: parse.
REASON_PAYER_NOT_SUPPORTED = "payer-has-no-prior-auth-api"
REASON_NO_PAYER_RECORDED = "no-payer-recorded"
REASON_NO_LAUNCH = "encounter-not-linked-to-ehr"
REASON_PATH_NOT_CONFIGURED = "submission-path-not-configured"


def choose_path(payer_name: str | None) -> tuple[str | None, str | None]:
    """Decide whether this payer has an automated submission path.

    **The payer name is normalised before the question is asked**, and this is
    the whole reason ``packages/payer-vocab`` exists. The stored name is the
    payer's own display spelling — "Aetna Better Health of MA", "AETNA" — because
    that is what goes to the payer, and comparing it against a slug set by string
    equality would answer "no automated path" for every payer whose name was
    spelled the way a ``Coverage`` resource spells it. That failure is silent and
    looks exactly like a payer genuinely outside the mandate.

    Args:
        payer_name: The payer's own display name, as stored.

    Returns:
        The canonical slug and, when there is no automated path, the reason.
        A slug with no reason means PAS is supported.
    """
    if not payer_name or not payer_name.strip():
        # TASK-060 assembles a bundle without a payer name rather than
        # discarding an encounter's findings, so this is reachable by design.
        return None, REASON_NO_PAYER_RECORDED

    slug = normalize_payer(payer_name)
    if not is_known_payer(slug):
        # Not an error and not a refusal: an unrecognised payer is routed on the
        # same rule as any other. Logged so "the name did not line up" is visible
        # in the trace instead of looking like a settled answer about the payer —
        # the distinction packages/payer-vocab exists to preserve.
        logger.warning(
            "Routing a prior authorization for unrecognised payer %r (slug %r)",
            payer_name,
            slug,
        )

    if not supports_prior_auth_api(slug):
        return slug, REASON_PAYER_NOT_SUPPORTED
    return slug, None


async def mark_manual_submission_required(
    session: AsyncSession,
    *,
    request_id: str,
    reason: str,
) -> None:
    """Record that this request has no automated path and needs a person.

    **The one thing this service writes about a submission**, and it is not a
    submission result: the columns a payer's answer lands in are written only by
    ``PATCH /prior-auth/{request_id}/submission`` on the owning service, and
    nothing here touches them. ``submission_method`` in particular stays NULL,
    because nothing transmitted anything.

    Written directly through the shared mapped classes rather than over HTTP,
    exactly as TASK-060 writes the row in the first place: ``fhir-integration``
    goes over HTTP because it deliberately holds no database connection, and this
    service holds one.

    Args:
        session: The session whose transaction the write joins.
        request_id: The request to mark.
        reason: A fixed label from this module, for the log line.
    """
    await session.execute(
        sa.update(PriorAuthRequest)
        .where(PriorAuthRequest.id == sa.cast(request_id, sa.Uuid))
        .values(status=PRIOR_AUTH_STATUS_MANUAL_REQUIRED)
    )
    await session.commit()
    logger.info(
        "Prior authorization request %s needs manual submission (%s); nothing was transmitted",
        request_id,
        reason,
    )


async def route_submission(
    session: AsyncSession,
    *,
    request_id: str,
    facts: submission_client.RoutingFacts,
) -> RoutingDecision:
    """Send this request down whichever path its payer supports.

    Args:
        session: The database session, for the manual-case write.
        request_id: The request being routed.
        facts: What the owning service said about it.

    Returns:
        What happened.

    Raises:
        submission_client.SubmissionUnavailable: The submitter or the payer could
            not be reached. Deliberately not converted into a manual outcome — "we
            could not ask" is not "there is nothing to ask", and recording the
            latter would quietly take a submittable request off the automated
            path because of a network blip.
        submission_client.SubmissionRefused: The submitter refused for a reason
            other than an unconfigured path.
    """
    slug, reason = choose_path(facts.payer_name)

    if reason is None and facts.launch_id is None:
        # An encounter with no SMART launch has no EHR credential, so there is
        # nothing to submit with. TASK-060 does not assemble a bundle for one,
        # so this is a broken invariant rather than an ordinary branch — but it
        # is recorded as manual rather than raised, because the request exists
        # and a person can still file it.
        logger.error(
            "Prior authorization request %s has no launch_id; TASK-060 should not "
            "have assembled a bundle for an encounter that is not linked to an EHR",
            request_id,
        )
        reason = REASON_NO_LAUNCH

    if reason is not None:
        await mark_manual_submission_required(session, request_id=request_id, reason=reason)
        return RoutingDecision(
            outcome=RoutingOutcome.MANUAL_REQUIRED,
            payer_slug=slug,
            reason=reason,
        )

    settings = get_settings()
    assert facts.launch_id is not None  # noqa: S101 — narrowed by the check above
    try:
        result = await submission_client.submit_through_fhir_integration(
            request_id,
            launch_id=facts.launch_id,
            base_url=settings.fhir_integration_url,
            timeout_seconds=settings.submission_timeout_seconds,
        )
    except submission_client.SubmissionRefused as exc:
        if exc.code != submission_client.ERROR_CODE_PATH_NOT_CONFIGURED:
            raise
        # The payer takes PAS, but this EHR delivers through CoverMyMeds and
        # nothing configured it. That is the same outcome as a payer with no
        # automated path, arriving one layer down — and never a retry, because no
        # amount of retrying configures a credential.
        await mark_manual_submission_required(
            session,
            request_id=request_id,
            reason=REASON_PATH_NOT_CONFIGURED,
        )
        return RoutingDecision(
            outcome=RoutingOutcome.MANUAL_REQUIRED,
            payer_slug=slug,
            reason=REASON_PATH_NOT_CONFIGURED,
        )

    logger.info(
        "Prior authorization request %s submitted for payer %s by %s; payer said %s",
        request_id,
        slug,
        result.submission_method,
        result.outcome,
    )
    return RoutingDecision(
        outcome=RoutingOutcome.SUBMITTED,
        payer_slug=slug,
        result=result,
    )

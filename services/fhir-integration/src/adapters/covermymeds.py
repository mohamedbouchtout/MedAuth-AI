"""The CoverMyMeds submission path — a seam, and an explicitly unverified mapping.

Athenahealth does not support FHIR PAS, so ``AthenaAdapter`` submits prior
authorizations here instead. What this module *is* — a second submission path
behind the same adapter method, configured rather than hardcoded, answering in
the same normalized :class:`~src.adapters.models.PriorAuthSubmission` as the FHIR
path — is TASK-054's scope and is built.

**THE FIELD MAPPING BELOW IS UNVERIFIED AND HAS NEVER BEEN CHECKED AGAINST THE
REAL COVERMYMEDS API.** This is stated here, in the module that holds it, rather
than in a task file, because that is where someone about to trust it will be.
Specifically, none of the following has been checked against anything:

* **the request shape** — the field names, their nesting, and whether a
  submission is a single POST at all;
* **the path** ``/prior-authorization-requests``;
* **the authentication scheme** — a bearer API key is assumed;
* **the response shape**, including where a reference number appears;
* **the status vocabulary** in :data:`OUTCOME_BY_STATUS`, which is the piece
  most likely to be wrong and the one with the worst failure: a status this
  mapping does not know is treated as unknown rather than guessed at, so a real
  vocabulary mismatch surfaces as a refusal to record rather than as a
  confidently wrong outcome.

There is no cheap way to falsify any of it. Unlike TASK-013's CMS export or
TASK-053's LOINC lookup, CoverMyMeds publishes no open schema and no public
sandbox, so a complete translation presented as working would be a fiction with
nothing to check it against. **Widening this is its own task, gated on sandbox
credentials** (TASK-055's neighbourhood), and it should begin by deleting these
guesses rather than by extending them.

What is *not* guesswork, and must survive any rewrite: the codes are filtered
before they leave (a payer is told what the provider documented, never what a
machine suggested), an unknown status is never reported as an outcome, and no
credential or patient detail reaches a log line.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import httpx

from .errors import FHIRAuthorizationExpired, FHIRMalformedResponse, FHIRUpstreamUnavailable
from .models import PriorAuthContent, PriorAuthSubmission, SubmissionMethod, SubmissionOutcome
from .outbound_codes import sendable_codes

logger = logging.getLogger(__name__)

#: Per-call timeout. The same value the FHIR calls use, for the same reason: this
#: is an external vendor rather than a service on our own network.
COVERMYMEDS_TIMEOUT_SECONDS: Final = 10.0

#: Unverified — see the module docstring.
SUBMIT_PATH: Final = "/prior-authorization-requests"

#: Unverified — see the module docstring. Absent from the map means *unknown*,
#: which is reported as a malformed response rather than mapped to an outcome:
#: recording a guess as the payer's answer is the failure this module is most
#: exposed to, and the one with no way to detect it later.
OUTCOME_BY_STATUS: Final[dict[str, SubmissionOutcome]] = {
    "approved": SubmissionOutcome.COMPLETE,
    "denied": SubmissionOutcome.COMPLETE,
    "completed": SubmissionOutcome.COMPLETE,
    "pending": SubmissionOutcome.QUEUED,
    "new": SubmissionOutcome.QUEUED,
    "sent": SubmissionOutcome.QUEUED,
    "partial": SubmissionOutcome.PARTIAL,
    "error": SubmissionOutcome.ERROR,
    "rejected": SubmissionOutcome.ERROR,
}


class CoverMyMedsNotConfigured(Exception):
    """The CoverMyMeds path was taken with no base URL or key configured.

    Its own exception rather than an empty-host HTTP call, which is what the
    settings' docstring promises: an unconfigured path says so plainly instead of
    failing somewhere inside a request to nowhere.
    """


class CoverMyMedsClient:
    """One HTTP call to CoverMyMeds, and the mapping around it.

    Holds no state beyond its configuration. The HTTP client is the process-wide
    pooled one, injected exactly as it is into an adapter and for the same
    reasons.
    """

    def __init__(self, base_url: str, api_key: str, http_client: httpx.AsyncClient) -> None:
        """Bind the client to the vendor's endpoint.

        Args:
            base_url: Where CoverMyMeds answers. Empty means not configured.
            api_key: The credential. Never logged, never rendered.
            http_client: The shared, pooled HTTP client.
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = http_client

    def __repr__(self) -> str:
        """Render without the API key — an adapter reaches log lines a key must not."""
        return f"{type(self).__name__}(base_url={self._base_url!r})"

    @property
    def is_configured(self) -> bool:
        """Whether both halves of the configuration are present."""
        return bool(self._base_url and self._api_key)

    def build_request(self, content: PriorAuthContent) -> dict[str, Any]:
        """Translate the request into what this vendor is *assumed* to want.

        **Unverified — see the module docstring.** The one part of this that is
        not a guess is the filter: only codes a provider stated or accepted leave
        this system, per CLAUDE.md, "Writing clinical data out to the EHR". Keep
        that when the rest is rewritten against a real schema.

        Args:
            content: The request in this system's terms, codes unfiltered.

        Returns:
            The JSON body to post.
        """
        coverage = content.coverage
        return {
            "request_id": content.request_id,
            "patient": {"ehr_id": content.patient_id},
            "encounter": {"ehr_id": content.encounter_id},
            "prescriber": {"fhir_reference": content.provider_reference},
            "payer": {
                "name": content.payer_name,
                "plan_type": coverage.plan_type if coverage else None,
                "member_id": coverage.member_id if coverage else None,
            },
            "services": [
                {"cpt_code": procedure.cpt_code, "description": procedure.description}
                for procedure in content.procedures
            ],
            "diagnoses": [
                {"icd10_code": code.code, "description": code.display}
                for code in sendable_codes(content.icd10_codes)
            ],
            "clinical_documentation": [
                {"text": evidence.text, "criterion": evidence.criterion}
                for evidence in content.clinical_evidence
            ],
        }

    async def submit(self, content: PriorAuthContent) -> PriorAuthSubmission:
        """Submit one prior authorization and report what came back.

        Args:
            content: The request in this system's terms.

        Returns:
            The normalized submission result, with ``submission_method`` set to
            ``covermymeds``.

        Raises:
            CoverMyMedsNotConfigured: No base URL or no API key.
            FHIRAuthorizationExpired: The vendor rejected the credential.
            FHIRUpstreamUnavailable: Transport failure, timeout or a 5xx.
            FHIRMalformedResponse: The vendor refused the request, or answered
                with a status this mapping does not know.
        """
        if not self.is_configured:
            raise CoverMyMedsNotConfigured(
                "COVERMYMEDS_BASE_URL and COVERMYMEDS_API_KEY are not both set"
            )

        try:
            response = await self._http.post(
                f"{self._base_url}{SUBMIT_PATH}",
                json=self.build_request(content),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=COVERMYMEDS_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            # Ambiguous exactly as a FHIR submission's timeout is: the vendor may
            # have taken the request. Never retried automatically.
            raise FHIRUpstreamUnavailable(
                "PriorAuthRequest",
                content.request_id,
                "CoverMyMeds did not answer in time",
                timed_out=True,
            ) from exc
        except httpx.HTTPError as exc:
            # Deliberately not ``str(exc)``: httpx puts the request URL in its
            # message, and the credential is on the request it describes.
            raise FHIRUpstreamUnavailable(
                "PriorAuthRequest", content.request_id, "CoverMyMeds could not be reached"
            ) from exc

        if response.status_code in (401, 403):
            raise FHIRAuthorizationExpired(
                "PriorAuthRequest", content.request_id, "CoverMyMeds rejected the API key"
            )
        if response.status_code >= 500:
            raise FHIRUpstreamUnavailable(
                "PriorAuthRequest",
                content.request_id,
                f"CoverMyMeds answered {response.status_code}",
            )
        if response.status_code >= 400:
            raise FHIRMalformedResponse(
                "PriorAuthRequest",
                content.request_id,
                f"CoverMyMeds refused the request ({response.status_code})",
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise FHIRMalformedResponse(
                "PriorAuthRequest", content.request_id, "CoverMyMeds' response was not JSON"
            ) from exc
        if not isinstance(body, dict):
            raise FHIRMalformedResponse(
                "PriorAuthRequest", content.request_id, "CoverMyMeds' response was not an object"
            )

        return PriorAuthSubmission(
            outcome=self._outcome(body, content.request_id),
            payer_reference_number=self._reference_number(body),
            submission_method=SubmissionMethod.COVERMYMEDS,
        )

    @staticmethod
    def _outcome(body: dict[str, Any], request_id: str) -> SubmissionOutcome:
        """Map the vendor's status onto a normalized outcome, or refuse to guess.

        A status outside :data:`OUTCOME_BY_STATUS` is a malformed response rather
        than a default. The mapping is unverified, so the value most likely to be
        wrong is exactly the one a default would silently paper over — and an
        outcome recorded wrongly is indistinguishable afterwards from one the
        payer actually gave.
        """
        status = body.get("status")
        outcome = OUTCOME_BY_STATUS.get(status) if isinstance(status, str) else None
        if outcome is None:
            # The status is the vendor's own vocabulary, not patient data, so it
            # is safe to name and it is the one thing a reader needs.
            logger.warning(
                "CoverMyMeds answered with a status this mapping does not know: %r", status
            )
            raise FHIRMalformedResponse(
                "PriorAuthRequest",
                request_id,
                "CoverMyMeds answered with a status this integration does not recognise",
            )
        return outcome

    @staticmethod
    def _reference_number(body: dict[str, Any]) -> str | None:
        """Read the vendor's reference for the submission, if the response has one.

        Unverified, and ``None`` is a legitimate answer here for the same reason
        it is on the FHIR path: a request the vendor has only queued may not have
        a reference yet.
        """
        for field in ("reference_number", "request_id", "id"):
            value = body.get(field)
            if isinstance(value, str) and value:
                return value
        return None

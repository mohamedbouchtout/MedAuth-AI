"""Reading a prior-authorization request from ``track-a-clinical``, and recording its result.

TASK-054, and the direct counterpart of ``notes_client.py`` one task earlier.
``POST /fhir/prior-auth`` needs what this service does not hold: the procedures
being requested, the diagnoses justifying them, the clinical evidence, the payer,
and the two EHR identifiers. All of it lives in ``track-a-clinical``, which owns
``prior_auth_requests`` and ``encounters``.

**It goes over HTTP even though both services share a virtualenv**, for the
reason the note client's docstring gives at length: reading this row is a PHI
access, the ``READ_PRIOR_AUTH`` row is written by that service's route layer, and
importing its storage functions here would read a patient's clinical evidence
with no audit row anywhere.

**A failure here is not the same as a failure at a payer.** Everything in this
module raises :class:`PriorAuthServiceError`, and the route maps it onto an
envelope outcome that says which side could not be reached. "Our own service is
down" and "the payer is down" send an operator to two different places.
"""

from __future__ import annotations

import logging
from typing import Any, Final, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from src.adapters.models import SubmissionMethod, SubmissionOutcome

logger = logging.getLogger(__name__)

#: Any of the payload models below, for :meth:`PriorAuthClient._parse`.
ModelT = TypeVar("ModelT", bound=BaseModel)

#: How the two routes are addressed. Kept here rather than formatted at the call
#: sites so a change to either path is one edit.
REQUEST_PATH: Final = "/prior-auth/{request_id}"
SUBMISSION_PATH: Final = "/prior-auth/{request_id}/submission"


class PriorAuthServiceError(Exception):
    """``track-a-clinical`` could not be reached, or did not answer usably.

    Attributes:
        detail: A fixed description of what failed. Never derived from a response
            body: that service's bodies carry transcript excerpts, and this
            message reaches an envelope.
        timed_out: Whether the call timed out, which the route turns into a 504
            rather than a 502.
        status_code: The status that came back, when one did. For logging and for
            the conflict case; never rendered into a client-facing message.
    """

    def __init__(self, detail: str, *, timed_out: bool = False, status_code: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.timed_out = timed_out
        self.status_code = status_code


class PriorAuthRequestNotFound(PriorAuthServiceError):
    """No such prior-authorization request.

    A distinct class because it is the one failure here that is not an
    infrastructure problem: the caller asked about something that does not exist,
    and retrying will not change that.
    """


class PriorAuthAlreadySubmitted(PriorAuthServiceError):
    """The owning service refused to record a second submission.

    Distinct because of *when* it can arrive. Before the payer is called it means
    this route's own pre-check was raced and nothing was sent. After the payer
    has answered it means something far worse — a real submission exists that
    nothing here has a record of — and the route says so rather than reporting a
    generic failure that invites a retry.
    """


class StoredPriorAuthProcedure(BaseModel):
    """One procedure, as the stored row carries it.

    A local mirror of the JSONB shape rather than an import, on the same terms as
    the note payload mirrors: the wire contract binds the two services, and
    importing across the boundary would make a deployment of one require a
    redeploy of the other.

    Attributes:
        cpt_code: The five-character CPT code.
        description: The procedure in the clinician's own words, when recorded.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    cpt_code: str
    description: str | None = None


class StoredDiagnosis(BaseModel):
    """One entry of ``diagnoses``, as the stored row carries it.

    ``source`` is required rather than optional because it is the field that
    decides whether the code may leave the system at all — a payload without one
    is malformed, not a code of unknown provenance to be sent anyway.

    Attributes:
        code: The code itself, dotted as stored.
        display: The source's own description, when it carried one.
        source: Which pass proposed this code, or that a provider accepted it.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    code: str
    display: str | None = None
    source: str


class StoredEvidence(BaseModel):
    """One excerpt of clinical documentation, as the stored row carries it.

    Attributes:
        text: The excerpt itself. A transcript excerpt, and PHI.
        criterion: The payer criterion it is offered against, when the gap
            analysis tied it to one.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    text: str
    criterion: str | None = None


class StoredPriorAuthRequest(BaseModel):
    """The request as ``GET /prior-auth/{request_id}`` returns it.

    Attributes:
        request_id: The row's primary key, echoed back.
        session_id: The encounter session this request came out of.
        patient_fhir_id: The patient as the EHR knows them.
        ehr_encounter_id: The encounter as the EHR knows it, or None when the
            visit was started outside a SMART launch.
        payer_name: The payer's own display name.
        insurance_plan_type: The plan type as the coverage spelled it.
        insurance_member_id: The member id the payer matches the request on.
        procedures: What is being requested. Empty is not a submittable request.
        diagnoses: The diagnoses justifying it, unfiltered — the builder filters.
        clinical_evidence: The documentation offered against the criteria.
        submitted_at: When it was transmitted, or None. **Non-null is what makes
            a repeat submission refusable** before a payer is ever called.
        payer_reference_number: The reference from a submission already made.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    request_id: str
    session_id: str
    patient_fhir_id: str
    ehr_encounter_id: str | None = None
    payer_name: str | None = None
    insurance_plan_type: str | None = None
    insurance_member_id: str | None = None
    procedures: list[StoredPriorAuthProcedure] | None = None
    diagnoses: list[StoredDiagnosis] | None = None
    clinical_evidence: list[StoredEvidence] | None = None
    submitted_at: str | None = None
    payer_reference_number: str | None = None


class PriorAuthClient:
    """The two calls the prior-auth submission makes to ``track-a-clinical``.

    Holds no state beyond its configuration. The HTTP client is the process-wide
    pooled one, injected exactly as it is into an adapter and for the same
    reasons.
    """

    def __init__(self, base_url: str, http_client: httpx.AsyncClient, timeout: float) -> None:
        """Bind the client to the service's address.

        Args:
            base_url: Where ``track-a-clinical`` answers.
            http_client: The shared, pooled HTTP client.
            timeout: Per-call timeout, set here rather than on the shared client.
        """
        self._base_url = base_url.rstrip("/")
        self._http = http_client
        self._timeout = timeout

    async def get_request(self, request_id: str) -> StoredPriorAuthRequest:
        """Read one prior-authorization request.

        This is a PHI read — the row carries transcript excerpts — and the
        ``READ_PRIOR_AUTH`` row for it is written by that service's route, which
        is the whole reason this call exists rather than an import.

        Raises:
            PriorAuthRequestNotFound: No such request.
            PriorAuthServiceError: The service could not be reached or did not
                answer usably.
        """
        body = await self._request("GET", REQUEST_PATH.format(request_id=request_id))
        return self._parse(StoredPriorAuthRequest, body, "prior authorization")

    async def record_submission(
        self,
        request_id: str,
        *,
        submission_method: SubmissionMethod,
        outcome: SubmissionOutcome,
        payer_reference_number: str | None,
    ) -> None:
        """Record what a payer said about a request this service submitted.

        **A 409 is raised as :class:`PriorAuthAlreadySubmitted`, not swallowed.**
        By the time this runs the payer has answered, so a refusal here means a
        real submission exists that this service cannot record — which the route
        reports rather than hides.

        Raises:
            PriorAuthAlreadySubmitted: The row was already marked submitted.
            PriorAuthServiceError: The record could not be written, for any
                other reason.
        """
        await self._request(
            "PATCH",
            SUBMISSION_PATH.format(request_id=request_id),
            json={
                "submission_method": submission_method.value,
                "outcome": outcome.value,
                "payer_reference_number": payer_reference_number,
            },
        )

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make one call and return the envelope's ``data``, or raise.

        Every failure is turned into a :class:`PriorAuthServiceError` here so no
        caller has to interpret a status code, and so no response body can reach
        an error message — that service's bodies carry clinical evidence.
        """
        if not self._base_url:
            raise PriorAuthServiceError("track-a-clinical is not configured")

        try:
            response = await self._http.request(
                method, f"{self._base_url}{path}", json=json, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise PriorAuthServiceError(
                "track-a-clinical did not answer in time", timed_out=True
            ) from exc
        except httpx.HTTPError as exc:
            # Deliberately not ``str(exc)``: httpx puts the request URL in its
            # message, and these paths carry a request id.
            raise PriorAuthServiceError("track-a-clinical could not be reached") from exc

        if response.status_code == 404:
            raise PriorAuthRequestNotFound("no such prior authorization request", status_code=404)
        if response.status_code == 409:
            raise PriorAuthAlreadySubmitted(
                "the request has already been submitted", status_code=409
            )
        if response.status_code >= 400:
            logger.warning(
                "track-a-clinical answered %s for a prior-auth call to %s",
                response.status_code,
                path,
            )
            raise PriorAuthServiceError(
                "track-a-clinical refused the request", status_code=response.status_code
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise PriorAuthServiceError("track-a-clinical's response was not JSON") from exc

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise PriorAuthServiceError("track-a-clinical's response carried no data")
        return data

    @staticmethod
    def _parse(model: type[ModelT], body: dict[str, Any], what: str) -> ModelT:
        """Validate a payload, without letting the rejected values into the error.

        Pydantic echoes offending values in its message and these payloads carry
        transcript excerpts. The same rule, for the same reason, as the note
        client's ``_parse``.
        """
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise PriorAuthServiceError(
                f"track-a-clinical's {what} payload was not usable"
            ) from exc

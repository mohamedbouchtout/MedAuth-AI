"""Reading a stored note from ``track-a-clinical``, and recording what was filed.

TASK-053. ``POST /fhir/notes`` needs three things this service does not hold: the
note's text, the codes extracted from it, and the two identifiers that say which
chart entry it belongs to. All of them live in ``track-a-clinical``, which owns
the ``clinical_notes`` and ``encounters`` tables.

**It goes over HTTP even though both services share a virtualenv.** The same
arrangement, and the same argument, as ``track_a_clinical.coverage_context``
calling this service — with the direction reversed. Reading a note's content is a
PHI access, and the ``READ_NOTE`` row is written by that service's route layer;
importing its storage functions here would read a patient's note with no audit
row anywhere. See CLAUDE.md, "Writing clinical data out to the EHR".

**The browser is not asked to carry the note back either.** ``apps/web`` already
holds the note it is looking at, so ``POST /fhir/notes`` could have taken the
text in its body and saved a round trip. That would put a chart write's payload
under the control of the least trusted participant in it, and — the part that
settles it — it would produce no ``READ_NOTE`` row at all, because from
``track-a-clinical``'s point of view nothing was read.

**A failure here is not the same as a failure there.** Everything in this module
raises :class:`NoteServiceError`, and the route maps it onto an envelope outcome
that says which side could not be reached. It is deliberately never conflated
with a FHIR-layer failure: "our own service is down" and "the EHR is down" send
an operator to two different places.
"""

from __future__ import annotations

import logging
from typing import Any, Final, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

#: Any of the payload models below, for :meth:`NotesClient._parse`.
ModelT = TypeVar("ModelT", bound=BaseModel)

#: How the two routes are addressed. Kept as functions rather than formatted at
#: the call sites so that a change to either path is one edit.
NOTE_PATH: Final = "/notes/{session_id}"
EHR_REFERENCE_PATH: Final = "/notes/{session_id}/ehr-reference"


class NoteServiceError(Exception):
    """``track-a-clinical`` could not be reached, or did not answer usably.

    Attributes:
        detail: A fixed description of what failed. Never derived from a response
            body: an error body from that service can carry note content, and
            this message reaches an envelope. Same rule the adapter layer follows
            for an ``OperationOutcome``.
        timed_out: Whether the call timed out, which the route turns into a 504
            rather than a 502.
        status_code: The status that came back, when one did. For logging and for
            the not-found case; never rendered into a client-facing message.
    """

    def __init__(self, detail: str, *, timed_out: bool = False, status_code: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.timed_out = timed_out
        self.status_code = status_code


class NoteNotFound(NoteServiceError):
    """No such session, or no note generated for it yet.

    A distinct class because it is the one failure here that is *not* an
    infrastructure problem: the caller asked about something that does not
    exist, and retrying will not change that.
    """


class ExtractedCode(BaseModel):
    """One entry of ``icd10_codes`` or ``cpt_codes``, as that service returns it.

    A local mirror of the shape rather than an import, for the same reason
    ``track_a_clinical.coverage_context`` mirrors this service's response models:
    the wire contract is what binds the two, and importing across a service
    boundary would make a deployment of one require a redeploy of the other.

    Only the fields this service reads are modelled. ``source`` is the one that
    decides whether a code may leave the system at all, so it is required rather
    than optional — a payload without it is malformed, not a code with an unknown
    provenance to be sent anyway.

    Attributes:
        code: The code itself, dotted as stored.
        display: The source's own description, when it carried one.
        source: Which pass proposed this code, or that a provider accepted it.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    code: str
    display: str | None = None
    source: str


class StoredNote(BaseModel):
    """The note as ``GET /notes/{session_id}`` returns it.

    Attributes:
        soap_subjective: The subjective section, when the generation wrote one.
        soap_objective: The objective section.
        soap_assessment: The assessment section.
        soap_plan: The plan section.
        icd10_codes: The extracted diagnoses, or ``None`` when the extraction
            pass never answered. **``None`` and ``[]`` are different facts** and
            are kept apart here for the same reason they are kept apart in the
            column: one means "not determined", the other means "none found".
        cpt_codes: The extracted procedures, on the same terms.
        reviewed_by_provider: Whether a provider has attested to the note. It
            decides the written document's ``docStatus`` and nothing else — this
            service never sets it.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    soap_subjective: str | None = None
    soap_objective: str | None = None
    soap_assessment: str | None = None
    soap_plan: str | None = None
    icd10_codes: list[ExtractedCode] | None = None
    cpt_codes: list[ExtractedCode] | None = None
    reviewed_by_provider: bool = False


class NoteEhrReference(BaseModel):
    """The note's EHR linkage, as ``GET /notes/{session_id}/ehr-reference`` returns it.

    Attributes:
        ehr_encounter_id: The encounter as the EHR knows it, or ``None`` when the
            visit was started outside a SMART launch. The write-back refuses on a
            ``None`` rather than addressing a guessed chart entry.
        patient_fhir_id: The patient the document is about.
        ehr_document_ref_id: A document already filed for this note, or ``None``.
            Non-null is what makes a repeat write-back a 409, answered before the
            EHR is called at all.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    ehr_encounter_id: str | None = None
    patient_fhir_id: str
    ehr_document_ref_id: str | None = None


class NotesClient:
    """The three calls the note write-back makes to ``track-a-clinical``.

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

    async def get_note(self, session_id: str) -> StoredNote:
        """Read the stored SOAP note for one session.

        This is a PHI read, and the ``READ_NOTE`` row for it is written by
        ``track-a-clinical``'s route — which is the whole reason this call exists
        rather than an import.

        Raises:
            NoteNotFound: No such session, or no note generated yet.
            NoteServiceError: The service could not be reached or did not answer
                usably.
        """
        body = await self._request("GET", NOTE_PATH.format(session_id=session_id))
        return self._parse(StoredNote, body, "note")

    async def get_ehr_reference(self, session_id: str) -> NoteEhrReference:
        """Read which chart entry this note belongs to, and whether it was filed.

        Raises:
            NoteNotFound: No such session, or no note generated yet.
            NoteServiceError: The service could not be reached or did not answer
                usably.
        """
        body = await self._request("GET", EHR_REFERENCE_PATH.format(session_id=session_id))
        return self._parse(NoteEhrReference, body, "EHR reference")

    async def record_ehr_document_ref(self, session_id: str, document_id: str) -> None:
        """Record the ``DocumentReference`` this service filed.

        **A 409 is raised as an ordinary failure, not swallowed.** It means
        another writer filed this note between our check and our write, and the
        caller has by then created a second document on the chart — which is
        exactly the situation the route reports rather than hides.

        Raises:
            NoteServiceError: The record could not be written, for any reason.
        """
        await self._request(
            "PATCH",
            EHR_REFERENCE_PATH.format(session_id=session_id),
            json={"ehr_document_ref_id": document_id},
        )

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make one call and return the envelope's ``data``, or raise.

        Every failure is turned into a :class:`NoteServiceError` here so no
        caller has to interpret a status code, and so no response body can reach
        an error message — that service's bodies carry note text.
        """
        if not self._base_url:
            raise NoteServiceError("track-a-clinical is not configured")

        try:
            response = await self._http.request(
                method, f"{self._base_url}{path}", json=json, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise NoteServiceError(
                "track-a-clinical did not answer in time", timed_out=True
            ) from exc
        except httpx.HTTPError as exc:
            # Deliberately not ``str(exc)``: httpx puts the request URL in its
            # message, and these paths carry a session id.
            raise NoteServiceError("track-a-clinical could not be reached") from exc

        if response.status_code == 404:
            raise NoteNotFound("no such session, or no note generated for it yet", status_code=404)
        if response.status_code >= 400:
            logger.warning(
                "track-a-clinical answered %s for a note write-back call to %s",
                response.status_code,
                path,
            )
            raise NoteServiceError(
                "track-a-clinical refused the request", status_code=response.status_code
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise NoteServiceError("track-a-clinical's response was not JSON") from exc

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise NoteServiceError("track-a-clinical's response carried no data")
        return data

    @staticmethod
    def _parse(model: type[ModelT], body: dict[str, Any], what: str) -> ModelT:
        """Validate a payload, without letting the rejected values into the error.

        Pydantic echoes offending values in its message and these payloads are a
        patient's note. The same rule, for the same reason, as the adapter
        layer's ``_parse``.
        """
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise NoteServiceError(f"track-a-clinical's {what} payload was not usable") from exc

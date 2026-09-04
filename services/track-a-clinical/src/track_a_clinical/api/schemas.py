"""Request and response bodies for the session, note and prior-authorization routes."""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from track_a_clinical.models import (
    ClinicalNote,
    Encounter,
    ExtractedCode,
    PriorAuthRequest,
    SubmissionMethod,
    SubmissionOutcome,
    load_codes,
)


class StartSessionRequest(BaseModel):
    """Body of ``POST /sessions/start``.

    ``patient_id`` is the wire name for what the schema stores as
    ``encounters.patient_fhir_id`` — the names differ deliberately, see CLAUDE.md
    "Session Lifecycle & JWT Issuance". No ``session_id`` field exists: the
    server generates it, and accepting a client-supplied one would let a caller
    collide with or impersonate another encounter's session.

    **``launch_id`` is accepted and ``session_id`` is not, and that asymmetry is
    the point.** A ``launch_id`` names a SMART launch the client genuinely holds
    — ``GET /fhir/callback`` handed it one — and it is what lets TASK-052b read
    the encounter's payer and site of care from the EHR. A ``session_id`` names
    this visit, which does not exist until this call answers. The two are
    different identifiers with different lifetimes and neither is derivable from
    the other; see CLAUDE.md, "A SMART launch is not an encounter session".
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: str = Field(min_length=1, max_length=100)
    provider_id: uuid.UUID
    ehr_encounter_id: str | None = Field(default=None, max_length=100)
    #: The SMART launch this visit was started from, when there was one. Both
    #: this and ``ehr_encounter_id`` are needed to read the payer columns: the
    #: launch supplies the EHR credential and the encounter id says which visit
    #: to ask about. Either alone leaves the columns NULL.
    launch_id: str | None = Field(default=None, max_length=64)


class ResolveProviderRequest(BaseModel):
    """Body of ``POST /providers/resolve``.

    One field, and it is the practitioner reference **as a verified ``fhirUser``
    claim gave it** — normally an absolute URL. Not a bare id: a ``Practitioner``
    id is unique only within one EHR, so ``Practitioner/1`` from two servers
    names two people, and accepting bare ids would merge them into one provider.

    This route is server-to-server. No browser calls it, and no client ever holds
    a practitioner reference — ``GET /fhir/launch-context`` hands an app the
    resolved ``provider_id`` instead, so nothing outside this network can assert
    a provider identity. See CLAUDE.md, "Provider identity — the registry that
    resolves an EHR practitioner".
    """

    model_config = ConfigDict(extra="forbid")

    fhir_practitioner_ref: str = Field(min_length=1, max_length=512)


class ProviderData(BaseModel):
    """``data`` payload returned by ``POST /providers/resolve``.

    Only the identifier the caller asked for. The reference it was resolved from
    is not echoed: the caller sent it and learns nothing from seeing it again.
    """

    provider_id: uuid.UUID


class StartSessionData(BaseModel):
    """``data`` payload returned by ``POST /sessions/start``."""

    session_id: uuid.UUID
    jwt: str


class EndSessionData(BaseModel):
    """``data`` payload returned by ``POST /sessions/{session_id}/end``.

    ``already_ended`` is true when the call was a no-op repeat. Callers do not
    need it to behave correctly — the endpoint is idempotent either way — but it
    makes a duplicate client retry visible instead of silent.
    """

    session_id: uuid.UUID
    status: str
    ended_at: datetime.datetime
    already_ended: bool


class RemintTokenData(BaseModel):
    """``data`` payload returned by ``POST /sessions/{session_id}/token``.

    Structurally identical to :class:`StartSessionData` and deliberately a
    separate model: the published spec should not describe a re-mint response as
    "StartSessionData", when the entire point of the endpoint is that it starts
    nothing. ``session_id`` is echoed back so a client can assert it got a token
    for the session it asked about rather than a new one.
    """

    session_id: uuid.UUID
    jwt: str


class NoteData(BaseModel):
    """``data`` payload returned by both ``/notes/{session_id}`` routes.

    Keyed on ``session_id`` rather than the note's own primary key or its
    encounter's: that is the only identifier a client has ever been given. The
    note id is included anyway because it is what the audit trail records, so a
    provider reporting a problem can name the row.

    ``icd10_codes`` and ``cpt_codes`` are ``None`` when the extraction pass never
    answered and ``[]`` when it ran and found nothing. The distinction survives
    onto the wire deliberately — a review screen must not show "no diagnoses"
    for a note whose codes were never determined.
    """

    session_id: uuid.UUID
    note_id: uuid.UUID
    soap_subjective: str | None
    soap_objective: str | None
    soap_assessment: str | None
    soap_plan: str | None
    icd10_codes: list[ExtractedCode] | None
    cpt_codes: list[ExtractedCode] | None
    generated_at: datetime.datetime
    reviewed_by_provider: bool
    provider_edited: bool
    ehr_document_ref_id: str | None

    @classmethod
    def from_row(cls, *, session_id: uuid.UUID, note: ClinicalNote) -> NoteData:
        """Render a stored row, preserving the null/empty distinction on both code columns."""
        return cls(
            session_id=session_id,
            note_id=note.id,
            soap_subjective=note.soap_subjective,
            soap_objective=note.soap_objective,
            soap_assessment=note.soap_assessment,
            soap_plan=note.soap_plan,
            icd10_codes=None if note.icd10_codes is None else load_codes(note.icd10_codes),
            cpt_codes=None if note.cpt_codes is None else load_codes(note.cpt_codes),
            generated_at=note.generated_at,
            reviewed_by_provider=note.reviewed_by_provider,
            provider_edited=note.provider_edited,
            ehr_document_ref_id=note.ehr_document_ref_id,
        )


class UpdateNoteRequest(BaseModel):
    """Body of ``PATCH /notes/{session_id}`` — a partial edit.

    **Every field is optional, and an omitted field is not the same as a null
    one.** The handler builds its update from ``model_fields_set``, so a field
    the client never mentioned is left alone while a field explicitly set to
    ``null`` clears the column. Defaulting the code lists to ``None`` and reading
    that as "clear it" would let a provider correcting one sentence of the plan
    section wipe an encounter's diagnoses — see CLAUDE.md, "So an editing
    endpoint needs three states, not two". This is why the defaults below exist
    only to make the fields optional and are never themselves written.

    ``provider_edited`` is absent by design: the server decides it, from whether
    a content field actually changed. ``reviewed_by_provider`` is present by
    design: it records a provider's attestation, so it has to be something the
    provider states rather than something a page load implies.

    ``extra="forbid"`` keeps a client from believing it edited ``generated_at``
    or ``ehr_document_ref_id`` — both server-owned — by sending them.
    """

    model_config = ConfigDict(extra="forbid")

    soap_subjective: str | None = None
    soap_objective: str | None = None
    soap_assessment: str | None = None
    soap_plan: str | None = None
    icd10_codes: list[ExtractedCode] | None = None
    cpt_codes: list[ExtractedCode] | None = None
    reviewed_by_provider: bool | None = None

    @model_validator(mode="after")
    def _reject_an_empty_patch(self) -> Self:
        """Reject a body that sets nothing.

        A ``PATCH`` carrying no fields is a client bug — most likely one that
        built its body from a diff and found no changes — and answering 200 to it
        would write an audit row implying an edit that was never attempted.
        """
        if not self.model_fields_set:
            raise ValueError(
                "a note edit must set at least one field; an empty body changes "
                "nothing and is more likely a client bug than an intent"
            )
        return self

    def edited_fields(self) -> dict[str, Any]:
        """Return only what the request actually carried, keyed by column name.

        ``reviewed_by_provider`` maps to the column of the same name; the SOAP
        and code fields already do. Values keep their parsed types —
        :class:`ExtractedCode` entries stay models — so the storage layer decides
        how they are serialised for JSONB.
        """
        return {name: getattr(self, name) for name in self.model_fields_set}


class NoteEhrReferenceData(BaseModel):
    """The EHR linkage of one session's note — what a write-back needs and produces.

    A sub-resource of the note rather than fields added to :class:`NoteData`,
    because the two answer different questions for different callers.
    :class:`NoteData` is what a provider's browser reads: clinical content and
    review flags. This is the join between our record of a visit and the EHR's,
    and its only consumer is ``fhir-integration``'s note write-back (TASK-053),
    which needs the two identifiers below in order to address a chart at all.

    Widening ``NoteData`` with them was the alternative. It was not taken because
    it would put encounter identifiers into a note payload for every browser
    read, to serve one server-to-server caller — and because the write-back
    genuinely wants a *second* fact ``NoteData`` should never carry: whether this
    note has already been filed, which is what makes a repeat write refusable.

    Attributes:
        session_id: The session this note belongs to, echoed so a caller holding
            several in flight cannot mismatch a response to a request.
        ehr_encounter_id: The encounter as the EHR knows it, from the
            ``encounters`` row. **Null is an ordinary state**: a visit started
            outside a SMART launch has no chart entry to file against, and the
            write-back refuses rather than guessing one.
        patient_fhir_id: The patient as the EHR knows them — the subject any
            written ``DocumentReference`` is about.
        ehr_document_ref_id: The document already filed for this note, or null
            when none has been. Non-null is what makes a second write-back a 409.
    """

    model_config = ConfigDict(frozen=True)

    session_id: uuid.UUID
    ehr_encounter_id: str | None
    patient_fhir_id: str
    ehr_document_ref_id: str | None

    @classmethod
    def from_rows(cls, *, encounter: Encounter, note: ClinicalNote) -> NoteEhrReferenceData:
        """Render the linkage from the two rows that hold it."""
        return cls(
            session_id=encounter.session_id,
            ehr_encounter_id=encounter.ehr_encounter_id,
            patient_fhir_id=encounter.patient_fhir_id,
            ehr_document_ref_id=note.ehr_document_ref_id,
        )


class RecordEhrReferenceRequest(BaseModel):
    """Body of ``PATCH /notes/{session_id}/ehr-reference``.

    **The transition is named explicitly rather than implied by an empty body.**
    A state-changing PATCH says what it is recording, so a reader of the wire
    format can tell what happened without knowing which route was called.

    This is the one and only way ``clinical_notes.ehr_document_ref_id`` is ever
    set. ``PATCH /notes/{session_id}`` still forbids the field, and that has not
    been relaxed: a provider's browser must not be able to claim a note was filed
    to a chart. This route is server-to-server, called by ``fhir-integration``
    once it has actually created the document.

    Attributes:
        ehr_document_ref_id: The id of the ``DocumentReference`` the EHR created.
    """

    model_config = ConfigDict(extra="forbid")

    ehr_document_ref_id: str = Field(min_length=1, max_length=100)


class PriorAuthRequestData(BaseModel):
    """``data`` payload of ``GET /prior-auth/{request_id}`` (TASK-054).

    Everything ``fhir-integration`` needs in order to submit a request it did not
    assemble, and nothing else. It is deliberately not the whole row: ``status``,
    the submission fields and the two EHR identifiers are here because the
    submitter branches on them, while ``decided_at`` and ``denial_reason`` belong
    to work that follows a decision up and would be a wider disclosure for no
    caller.

    **Keyed on the request's own primary key**, unlike every note route, and the
    difference is not an inconsistency. ``nudge_id`` set the precedent: a route
    is keyed on the identifier its caller was actually handed, and one encounter
    can carry several prior-authorization requests, so a ``session_id`` would not
    name one. See CLAUDE.md, "A route keyed on a resource rather than a session".

    The three JSONB columns are passed through as they are stored rather than
    reshaped here. They are written by TASK-060, which does not exist, so a shape
    asserted at this boundary today would be one this service invented — and the
    submitter's builder is where a shape is actually required and can be
    enforced.

    Attributes:
        request_id: The row's primary key, echoed so a caller with several in
            flight can match a response to its request.
        session_id: The encounter session this request came out of.
        status: Where the request has got to in *our* process.
        payer_outcome: What the payer said, once it has been asked. Null before
            submission, and a different fact from ``status``.
        patient_fhir_id: The patient as the EHR knows them.
        ehr_encounter_id: The encounter as the EHR knows it, or null when the
            visit was started outside a SMART launch. Null is an ordinary state
            and the submitter decides what to do about it.
        payer_name: The payer's own display name — from the request row when
            TASK-060 recorded one, else the encounter's. **Never a payer_vocab
            slug**: a slug matches our indexed policies, and this value is going
            to the payer.
        insurance_plan_type: The plan type as the coverage spelled it.
        insurance_member_id: The member id the payer matches the request on.
        procedures: What is being requested, as stored.
        diagnoses: The diagnoses justifying it, as stored. Their ``source`` is
            what decides whether each may leave the system, and the filtering
            happens in the submitter's builder rather than here.
        clinical_evidence: The documentation offered against the payer's
            criteria. Transcript excerpts, and the most sensitive thing this
            payload carries.
        submission_method: How it went out, or null before it has.
        payer_reference_number: The payer's reference, when it gave one.
        submitted_at: When it was transmitted, or null. **Non-null is what makes
            a repeat submission refusable** before a payer is ever called.
    """

    model_config = ConfigDict(frozen=True)

    request_id: uuid.UUID
    session_id: uuid.UUID
    status: str
    payer_outcome: str | None
    patient_fhir_id: str
    ehr_encounter_id: str | None
    payer_name: str | None
    insurance_plan_type: str | None
    insurance_member_id: str | None
    procedures: list[dict[str, Any]] | None
    diagnoses: list[dict[str, Any]] | None
    clinical_evidence: list[dict[str, Any]] | None
    submission_method: str | None
    payer_reference_number: str | None
    submitted_at: datetime.datetime | None

    @classmethod
    def from_rows(cls, *, request: PriorAuthRequest, encounter: Encounter) -> PriorAuthRequestData:
        """Render the two rows that together describe one submittable request.

        ``payer_name`` falls back to the encounter's ``insurance_payer`` when the
        request row carries none. Both hold the payer's own display name — the
        encounter's is copied from the ``Coverage`` at launch — so the fallback
        joins two spellings of one fact rather than substituting a different one.
        """
        return cls(
            request_id=request.id,
            session_id=encounter.session_id,
            status=request.status,
            payer_outcome=request.payer_outcome,
            patient_fhir_id=encounter.patient_fhir_id,
            ehr_encounter_id=encounter.ehr_encounter_id,
            payer_name=request.payer_name or encounter.insurance_payer,
            insurance_plan_type=encounter.insurance_plan_type,
            insurance_member_id=encounter.insurance_member_id,
            procedures=request.procedures,
            diagnoses=request.diagnoses,
            clinical_evidence=request.clinical_evidence,
            submission_method=request.submission_method,
            payer_reference_number=request.payer_reference_number,
            submitted_at=request.submitted_at,
        )


class RecordSubmissionRequest(BaseModel):
    """Body of ``PATCH /prior-auth/{request_id}/submission`` (TASK-054).

    **The transition is named explicitly rather than implied by an empty body**,
    the same rule ``RecordEhrReferenceRequest`` follows: a state-changing PATCH
    says what it is recording.

    This is the only way ``submission_method``, ``payer_outcome`` and
    ``payer_reference_number`` are ever set. The route is server-to-server,
    called by ``fhir-integration`` once a payer has actually answered, which is
    what makes the values it carries trustworthy.

    **The vocabularies are validated here as well as at the column.** Both are
    ``StrEnum`` fields, so a value outside them is a 422 with a field location
    rather than a 500 from the mapped class's validator — which is the backstop
    for the callers static typing does not reach, not the first line of defence.

    Attributes:
        submission_method: Which path transmitted the request.
        outcome: What the payer said. Required, because every path has an answer
            and a submission recorded without one would let a caller read a
            rejection as a pending request.
        payer_reference_number: The payer's reference, when it gave one.
            **Optional on purpose**: ``ClaimResponse.preAuthRef`` is 0..1 and is
            legitimately absent on a queued answer, so requiring it would refuse
            to record exactly the submissions most in need of following up.
    """

    model_config = ConfigDict(extra="forbid")

    submission_method: SubmissionMethod
    outcome: SubmissionOutcome
    payer_reference_number: str | None = Field(default=None, min_length=1, max_length=200)

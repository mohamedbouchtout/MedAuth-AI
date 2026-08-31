"""Request and response bodies for the session lifecycle and note review endpoints."""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from track_a_clinical.models import ClinicalNote, ExtractedCode, load_codes


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

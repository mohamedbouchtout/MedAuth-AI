"""Composing the ``DocumentReference`` a note is filed as. TASK-053.

The rule this file exists to protect is the first one below: a code no provider
ever stated must not reach a patient's chart. Everything else here is about
being conformant enough that a real EHR accepts the document, and honest enough
that a clinician reading it knows whether it has been signed.
"""

from __future__ import annotations

import base64

import pytest

from src.adapters.models import ClinicalNoteContent, NoteCode
from src.adapters.note_document import (
    CATEGORY_CODE,
    ICD10_HEADING,
    LOINC_SYSTEM,
    NOTE_TYPE_CODE,
    build_document_reference,
    render_note_text,
    sendable_codes,
)

LLM_CODE = NoteCode(
    code="M17.11", display="Unilateral primary osteoarthritis, right knee", source="llm-extraction"
)
SUGGESTED_CODE = NoteCode(
    code="E11.9",
    display="Type 2 diabetes mellitus without complications",
    source="comprehend-medical",
)
ACCEPTED_CODE = NoteCode(code="M25.561", display="Pain in right knee", source="provider-accepted")


def make_note(**overrides: object) -> ClinicalNoteContent:
    """A note with all four sections and one code of each source."""
    fields: dict[str, object] = {
        "patient_id": "patient-7",
        "encounter_id": "encounter-4",
        "subjective": "Right knee pain for three months.",
        "objective": "Tenderness over the medial joint line.",
        "assessment": "Likely primary osteoarthritis of the right knee.",
        "plan": "Order MRI right knee.",
        "icd10_codes": [LLM_CODE, SUGGESTED_CODE, ACCEPTED_CODE],
        "reviewed_by_provider": False,
    }
    return ClinicalNoteContent(**(fields | overrides))  # type: ignore[arg-type]


def written_text(note: ClinicalNoteContent) -> str:
    """Decode the attachment of the document built for `note`."""
    document = build_document_reference(note)
    data = document.content[0].attachment.data
    assert data is not None
    return base64.b64decode(data).decode("utf-8")


def test_a_machine_suggestion_is_not_sendable() -> None:
    """The rule this module exists for: only a stated or accepted code leaves.

    A ``comprehend-medical`` entry is a code the validating pass surfaced that no
    provider ever stated. It is a suggestion, and a patient's permanent chart is
    not where a suggestion belongs.
    """
    assert sendable_codes([LLM_CODE, SUGGESTED_CODE, ACCEPTED_CODE]) == [LLM_CODE, ACCEPTED_CODE]


@pytest.mark.parametrize("codes", [None, []])
def test_absent_and_empty_codes_both_send_nothing(codes: list[NoteCode] | None) -> None:
    """They mean different things upstream; neither yields a documented code."""
    assert sendable_codes(codes) == []


def test_a_suggested_code_never_reaches_the_document() -> None:
    """The end-to-end version of the filter, asserted on the written bytes.

    Testing :func:`sendable_codes` alone would not catch a builder that stopped
    calling it, which is the way this rule would actually be lost.
    """
    text = written_text(make_note())

    assert SUGGESTED_CODE.code not in text
    assert SUGGESTED_CODE.display is not None
    assert SUGGESTED_CODE.display not in text
    assert LLM_CODE.code in text
    assert ACCEPTED_CODE.code in text


def test_the_note_body_carries_every_section() -> None:
    text = written_text(make_note())

    for heading in ("Subjective", "Objective", "Assessment", "Plan"):
        assert heading in text
    assert "Right knee pain for three months." in text


def test_a_section_that_was_never_generated_is_omitted() -> None:
    """Not rendered as an empty heading, which reads as a section with no findings."""
    text = written_text(make_note(objective=None))

    assert "Objective" not in text
    assert "Subjective" in text


def test_the_code_heading_is_omitted_when_nothing_is_sendable() -> None:
    """A heading with nothing under it reads as "no diagnoses", which is a different claim."""
    text = written_text(make_note(icd10_codes=[SUGGESTED_CODE]))

    assert ICD10_HEADING not in text


def test_the_note_type_is_a_progress_note() -> None:
    """LOINC 11506-3, not 11488-4.

    A consult note is the response to another clinician's request for an
    opinion; an ambient office visit is a progress note. Both are in US Core's
    common set, so a server would have accepted either without complaint — which
    is exactly why this is asserted rather than left to a reviewer's eye.
    """
    document = build_document_reference(make_note())

    assert document.type is not None
    coding = document.type.coding
    assert coding is not None
    assert coding[0].system == LOINC_SYSTEM
    assert coding[0].code == NOTE_TYPE_CODE == "11506-3"


def test_a_category_is_always_sent() -> None:
    """US Core binds it as required and 1..*, so a document without one is non-conformant."""
    document = build_document_reference(make_note())

    assert document.category is not None
    coding = document.category[0].coding
    assert coding is not None
    assert coding[0].code == CATEGORY_CODE


@pytest.mark.parametrize(
    ("reviewed", "expected"),
    [(False, "preliminary"), (True, "final")],
)
def test_doc_status_reports_whether_a_provider_has_attested(reviewed: bool, expected: str) -> None:
    """An unreviewed note is filed, and says so, rather than being refused."""
    document = build_document_reference(make_note(reviewed_by_provider=reviewed))

    assert document.doc_status == expected
    assert document.status == "current"


def test_no_authenticator_is_asserted() -> None:
    """It names who attested, and this repository cannot verify one before Phase 5."""
    assert build_document_reference(make_note()).authenticator is None


def test_the_document_names_its_patient_and_encounter() -> None:
    """Both in the EHR's namespace — a session_id here would address nothing."""
    document = build_document_reference(make_note())

    assert document.subject is not None
    assert document.subject.reference == "Patient/patient-7"
    assert document.context is not None
    assert document.context.encounter is not None
    assert document.context.encounter[0].reference == "Encounter/encounter-4"


def test_the_body_round_trips_through_base64() -> None:
    """The attachment is the document; a mangled encoding is an unreadable chart entry."""
    note = make_note(subjective="Ünicode — em dash, accented vowel.")

    assert "Ünicode — em dash, accented vowel." in written_text(note)


def test_the_rendered_text_is_what_gets_attached() -> None:
    """One composition, not two: the builder attaches exactly what the renderer made."""
    note = make_note()

    assert written_text(note) == render_note_text(note)

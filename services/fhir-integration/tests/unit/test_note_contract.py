"""The note service and this client have to agree — proven, not assumed (TASK-053).

``track-a-clinical`` serves the note and its EHR linkage; ``src/notes_client.py``
mirrors those two payloads so it can read them. Every other test in this suite
feeds that client a hand-written body, which proves it parses what the tests
expect but cannot prove it parses what the other service actually sends: both
sides can drift together into a shape no deployment produces.

This closes that gap by building the payloads from ``track-a-clinical``'s own
response models and validating them with this service's mirrors, with nothing
hand-written in between. The specific failure it exists to catch is a renamed or
retyped field on that side — the write-back would then read a note it could not
parse, and the only symptom would be a 502 from a service that is up.

**Why a mirror at all, rather than importing those models.** The wire contract is
what binds two services; importing across the boundary would make a deployment of
one require a redeploy of the other, which is the arrangement
``track_a_clinical.coverage_context`` already declined in the other direction.
The mirror is the deliberate choice, and this file is the price of it.

``.github/scripts/detect-changed-members.sh`` selects this service when
``track-a-clinical`` changes, so an edit to the issuing side re-runs this file
rather than leaving it decorative — the same arrangement as
``packages/session-auth``'s issuer contract test.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from src.notes_client import NoteEhrReference, StoredNote
from track_a_clinical.api.schemas import (
    NoteData,
    NoteEhrReferenceData,
    RecordEhrReferenceRequest,
)
from track_a_clinical.models import ClinicalNote, Encounter, ExtractedCode

SESSION_ID = uuid.uuid4()


def make_encounter() -> Encounter:
    encounter = Encounter(
        session_id=SESSION_ID,
        patient_fhir_id="patient-7",
        provider_id=uuid.uuid4(),
        status="completed",
    )
    encounter.id = uuid.uuid4()
    encounter.ehr_encounter_id = "encounter-4"
    return encounter


def make_note(encounter: Encounter, **overrides: object) -> ClinicalNote:
    note = ClinicalNote(
        encounter_id=encounter.id,
        soap_subjective="Right knee pain for three months.",
        soap_objective="Tenderness over the medial joint line.",
        soap_assessment="Likely primary osteoarthritis of the right knee.",
        soap_plan="Order MRI right knee.",
        icd10_codes=[
            {
                "code": "M17.11",
                "display": "Unilateral primary osteoarthritis, right knee",
                "source": "llm-extraction",
                "confidence": None,
                "validation": None,
            }
        ],
        cpt_codes=[],
    )
    note.id = uuid.uuid4()
    note.generated_at = datetime.datetime(2026, 8, 18, 12, 45, tzinfo=datetime.UTC)
    note.reviewed_by_provider = False
    note.provider_edited = False
    note.ehr_document_ref_id = None
    for field, value in overrides.items():
        setattr(note, field, value)
    return note


def served_note(note: ClinicalNote) -> dict[str, object]:
    """Exactly what ``GET /notes/{session_id}`` puts in its envelope's ``data``."""
    return NoteData.from_row(session_id=SESSION_ID, note=note).model_dump(mode="json")


def served_reference(encounter: Encounter, note: ClinicalNote) -> dict[str, object]:
    """Exactly what ``GET /notes/{session_id}/ehr-reference`` serves."""
    return NoteEhrReferenceData.from_rows(encounter=encounter, note=note).model_dump(mode="json")


def test_the_real_note_payload_parses() -> None:
    """Field for field, against what that service's own model produces."""
    encounter = make_encounter()
    note = make_note(encounter)

    parsed = StoredNote.model_validate(served_note(note))

    assert parsed.soap_subjective == note.soap_subjective
    assert parsed.soap_objective == note.soap_objective
    assert parsed.soap_assessment == note.soap_assessment
    assert parsed.soap_plan == note.soap_plan
    assert parsed.reviewed_by_provider is False


def test_the_code_shape_survives_the_boundary() -> None:
    """``source`` in particular: it is what decides whether a code may be sent.

    A rename on the issuing side would leave every code unsendable and the note
    filed with no diagnoses at all — a quiet wrong answer rather than a failure.
    """
    encounter = make_encounter()

    parsed = StoredNote.model_validate(served_note(make_note(encounter)))

    assert parsed.icd10_codes is not None
    assert parsed.icd10_codes[0].code == "M17.11"
    assert parsed.icd10_codes[0].source == "llm-extraction"
    assert parsed.icd10_codes[0].display == "Unilateral primary osteoarthritis, right knee"


def test_null_and_empty_code_lists_both_survive() -> None:
    """ "Not determined" and "none found" are different facts on both sides."""
    encounter = make_encounter()

    absent = StoredNote.model_validate(served_note(make_note(encounter, icd10_codes=None)))
    empty = StoredNote.model_validate(served_note(make_note(encounter, icd10_codes=[])))

    assert absent.icd10_codes is None
    assert empty.icd10_codes == []


def test_the_real_linkage_payload_parses() -> None:
    encounter = make_encounter()
    note = make_note(encounter)

    parsed = NoteEhrReference.model_validate(served_reference(encounter, note))

    assert parsed.ehr_encounter_id == "encounter-4"
    assert parsed.patient_fhir_id == "patient-7"
    assert parsed.ehr_document_ref_id is None


def test_an_unlinked_encounter_parses_as_a_null_rather_than_failing() -> None:
    """The write-back refuses on this, so it has to survive the boundary intact."""
    encounter = make_encounter()
    encounter.ehr_encounter_id = None

    parsed = NoteEhrReference.model_validate(served_reference(encounter, make_note(encounter)))

    assert parsed.ehr_encounter_id is None


def test_a_filed_note_reports_its_document_id() -> None:
    """What makes a repeat write-back refusable before the EHR is called."""
    encounter = make_encounter()
    note = make_note(encounter, ehr_document_ref_id="docref-9")

    parsed = NoteEhrReference.model_validate(served_reference(encounter, note))

    assert parsed.ehr_document_ref_id == "docref-9"


def test_the_recording_body_is_what_that_route_accepts() -> None:
    """The one payload this service sends rather than reads.

    Built here from that route's own request model, so a field renamed there
    fails this test instead of failing a live write-back after the document has
    already been filed.
    """
    body = {"ehr_document_ref_id": "docref-9"}

    assert RecordEhrReferenceRequest.model_validate(body).ehr_document_ref_id == "docref-9"


def test_an_extracted_code_from_that_service_keeps_its_three_sources() -> None:
    """The vocabulary the filter switches on, taken from the issuing side's model."""
    for source in ("llm-extraction", "comprehend-medical", "provider-accepted"):
        code = ExtractedCode.model_validate(
            {"code": "M17.11", "display": None, "source": source, "confidence": None}
        )
        assert code.source == source


@pytest.mark.parametrize("missing", ["code", "source"])
def test_a_code_without_its_identifying_fields_is_refused(missing: str) -> None:
    """Neither side treats an unattributed code as sendable-by-default."""
    payload = {"code": "M17.11", "source": "llm-extraction"}
    del payload[missing]

    with pytest.raises(ValueError):
        StoredNote.model_validate({"icd10_codes": [payload]})

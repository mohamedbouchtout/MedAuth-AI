"""Builders for the mapped rows these tests assemble bundles from.

The mapped classes come from ``track_a_clinical.models``, which is the point —
this service maps nothing of its own. Instances are built unattached to a
session, which is all the pure assembly functions need.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from track_a_clinical.models import (
    ENCOUNTER_STATUS_COMPLETED,
    SOURCE_COMPREHEND_MEDICAL,
    SOURCE_LLM_EXTRACTION,
    ClinicalNote,
    ClinicalNudge,
    Encounter,
)

#: A note whose sections mention two procedures by name, so the excerpt matcher
#: has something realistic to narrow. Deliberately written the way a generated
#: SOAP note reads, including a sentence about an unrelated procedure that must
#: not leak into another procedure's evidence.
SUBJECTIVE = (
    "Patient reports right knee pain for four months. "
    "She has completed six weeks of physical therapy without relief. "
    "No prior imaging of the shoulder."
)
OBJECTIVE = "Right knee shows medial joint line tenderness. Shoulder exam is unremarkable."
ASSESSMENT = "Right knee internal derangement. Ordering a knee MRI to characterise the tear."
PLAN = "Knee MRI as above. Continue NSAIDs. Follow up in three weeks."


def an_encounter(
    *,
    launch_id: str | None = "launch-abc",
    insurance_payer: str | None = "Aetna",
    patient_fhir_id: str = "patient-1",
    provider_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
) -> Encounter:
    """Return an unattached completed encounter."""
    encounter = Encounter(
        session_id=session_id or uuid.uuid4(),
        patient_fhir_id=patient_fhir_id,
        provider_id=provider_id or uuid.uuid4(),
        launch_id=launch_id,
        ehr_encounter_id="Encounter/9",
        status=ENCOUNTER_STATUS_COMPLETED,
        insurance_payer=insurance_payer,
    )
    encounter.id = uuid.uuid4()
    return encounter


def a_note(
    *,
    icd10_codes: list[dict[str, Any]] | None = None,
    subjective: str | None = SUBJECTIVE,
    objective: str | None = OBJECTIVE,
    assessment: str | None = ASSESSMENT,
    plan: str | None = PLAN,
) -> ClinicalNote:
    """Return an unattached note with the given sections and codes."""
    note = ClinicalNote(
        encounter_id=uuid.uuid4(),
        soap_subjective=subjective,
        soap_objective=objective,
        soap_assessment=assessment,
        soap_plan=plan,
        icd10_codes=icd10_codes,
    )
    note.id = uuid.uuid4()
    return note


def a_nudge(
    *,
    procedure_name: str | None = "knee MRI",
    cpt_code: str | None = "73721",
    missing_criteria: list[str] | None = None,
    nudge_message: str | None = "Prior authorization required for knee MRI.",
    fired_at: datetime.datetime | None = None,
) -> ClinicalNudge:
    """Return an unattached nudge."""
    nudge = ClinicalNudge(
        encounter_id=uuid.uuid4(),
        procedure_name=procedure_name,
        cpt_code=cpt_code,
        missing_criteria=missing_criteria,
        nudge_message=nudge_message,
        denial_risk="high",
    )
    nudge.id = uuid.uuid4()
    nudge.fired_at = fired_at or datetime.datetime.now(tz=datetime.UTC)
    return nudge


def a_code(
    code: str,
    *,
    source: str = SOURCE_LLM_EXTRACTION,
    display: str | None = "Unilateral primary osteoarthritis, right knee",
    confidence: float | None = None,
) -> dict[str, Any]:
    """Return one entry of the ``icd10_codes`` JSONB shape."""
    return {
        "code": code,
        "display": display,
        "source": source,
        "confidence": confidence,
        "validation": None,
    }


def a_suggested_code(code: str) -> dict[str, Any]:
    """Return a ``comprehend-medical`` entry — a machine suggestion, not a diagnosis."""
    return a_code(code, source=SOURCE_COMPREHEND_MEDICAL, confidence=0.91)

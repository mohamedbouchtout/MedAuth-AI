"""Turning a stored SOAP note into the ``DocumentReference`` that goes on a chart.

TASK-053. Kept out of ``base.py`` because two things here are rules rather than
resource plumbing, and both are easier to find — and harder to quietly drop —
under their own names:

* **Which codes may leave this system at all** (:func:`sendable_codes`).
* **What kind of document this is**, in a vocabulary US Core binds as required.

Everything in this module is PHI. Nothing here logs.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Final

from fhir_types import (
    Attachment,
    CodeableConcept,
    Coding,
    DocumentReference,
    DocumentReferenceContent,
    DocumentReferenceContext,
    Reference,
)

from .models import ClinicalNoteContent, NoteCode

#: The two ``source`` values a code may carry and still be written to a chart.
#:
#: A ``comprehend-medical`` entry is deliberately absent, and this is the rule
#: rather than a conservative default: that source means the validating pass
#: surfaced a code **no provider ever stated**. CLAUDE.md already forbids putting
#: one in a prior-auth bundle, and a patient's permanent chart is the more
#: consequential artifact of the two, so the rule applies here with more force.
#: The way such a code becomes sendable is unchanged — a provider accepts it
#: through ``PATCH /notes/{session_id}``, which rewrites its ``source`` to
#: ``provider-accepted``.
#:
#: Do not widen this set to "everything we extracted". The filter is the point.
SENDABLE_CODE_SOURCES: Final = frozenset({"llm-extraction", "provider-accepted"})

#: LOINC ``11506-3``, Progress note. **Not ``11488-4``, Consult note**, which an
#: earlier draft of TASK-053 named: a consult note is the response to another
#: clinician's request for an opinion, and what MedAuth records is an ambient
#: office visit. ONC's USCDI entry defines a progress note as representing "a
#: patient's interval status during a hospitalization, outpatient visit,
#: treatment with a post-acute care provider, or other healthcare encounter",
#: which is this exactly. Both codes are in US Core's "Common Clinical Notes"
#: set, so the wrong one would have been accepted by a server without complaint.
LOINC_SYSTEM: Final = "http://loinc.org"
NOTE_TYPE_CODE: Final = "11506-3"
NOTE_TYPE_DISPLAY: Final = "Progress note"

#: US Core binds ``category`` as required on its ``uscore`` slice and makes it
#: ``1..*``, so a document without one is non-conformant even where a server
#: accepts it. ``clinical-note`` is the value for everything this service writes.
CATEGORY_SYSTEM: Final = "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category"
CATEGORY_CODE: Final = "clinical-note"
CATEGORY_DISPLAY: Final = "Clinical Note"

#: Plain text rather than markdown or HTML: the note is rendered by whatever
#: chart viewer the practice uses, and plain text is the one format every one of
#: them displays without interpreting anything.
CONTENT_TYPE: Final = "text/plain; charset=utf-8"

#: How the codes are labelled in the document body. See :func:`render_note_text`
#: for why they are in the text at all.
ICD10_HEADING: Final = "ICD-10-CM codes"

_SECTION_HEADINGS: Final = (
    ("Subjective", "subjective"),
    ("Objective", "objective"),
    ("Assessment", "assessment"),
    ("Plan", "plan"),
)


def sendable_codes(codes: Sequence[NoteCode] | None) -> list[NoteCode]:
    """Return only the codes that may be written to an EHR.

    Args:
        codes: The note's extracted codes, or ``None`` when the extraction pass
            never answered. ``None`` and ``[]`` mean different things upstream —
            "not determined" against "none found" — but they produce the same
            answer here, because neither yields a code a provider documented.

    Returns:
        The entries whose ``source`` is in :data:`SENDABLE_CODE_SOURCES`, in the
        order given.
    """
    return [code for code in codes or () if code.source in SENDABLE_CODE_SOURCES]


def render_note_text(note: ClinicalNoteContent) -> str:
    """Compose the document body a clinician will read on the chart.

    **The codes are in the text, and that is a decision rather than a
    shortcut.** ``DocumentReference`` has no diagnosis element — it is a wrapper
    around a document, and FHIR's home for a coded diagnosis is ``Condition``.
    The two alternatives were rejected in TASK-053: ``context.event`` means
    *clinical acts the document describes*, so ICD-10 codes there assert
    something the element does not mean and no consumer reads as a problem list;
    and writing ``Condition`` resources adds entries to a patient's problem list,
    which is a clinical assertion a provider makes rather than a side effect of
    filing a note.

    A section the generation never wrote is omitted rather than rendered as an
    empty heading, and the codes are omitted entirely when none survives the
    filter — a heading with nothing under it reads as "no diagnoses" rather than
    as "none that may be sent".

    Args:
        note: The stored note, with its codes unfiltered.

    Returns:
        The note as plain text.
    """
    blocks = [
        f"{heading}\n{text.strip()}"
        for heading, attribute in _SECTION_HEADINGS
        if (text := getattr(note, attribute))
    ]

    codes = sendable_codes(note.icd10_codes)
    if codes:
        lines = "\n".join(
            f"{code.code} — {code.display}" if code.display else code.code for code in codes
        )
        blocks.append(f"{ICD10_HEADING}\n{lines}")

    return "\n\n".join(blocks)


def build_document_reference(note: ClinicalNoteContent) -> DocumentReference:
    """Build the ``DocumentReference`` to POST to an EHR.

    ``docStatus`` carries whether a provider has attested to the note:
    ``preliminary`` until they have, ``final`` once they have. Writing an
    unreviewed note is deliberately allowed — the chart is where a provider
    actually reviews and signs, and a provider blocked from filing would paste
    the text in by hand, which puts the same content on the chart with no
    provenance at all. FHIR has an element whose whole purpose is to say a
    document is not yet attested, and using it honestly beats either extreme.

    ``authenticator`` is deliberately unset. It names who attested to the
    document, and no provider authentication exists in this repository before
    Phase 5 — asserting an attester we cannot verify is the same fabrication as a
    service-account UUID in an audit row.

    Args:
        note: The note, its identifiers, and whether it has been reviewed.

    Returns:
        The resource to write. Not yet sent — :meth:`EHRAdapter.write_clinical_note`
        does that.
    """
    body = render_note_text(note)
    return DocumentReference(
        status="current",
        doc_status="final" if note.reviewed_by_provider else "preliminary",
        type=CodeableConcept(
            coding=[Coding(system=LOINC_SYSTEM, code=NOTE_TYPE_CODE, display=NOTE_TYPE_DISPLAY)],
            text=NOTE_TYPE_DISPLAY,
        ),
        category=[
            CodeableConcept(
                coding=[
                    Coding(system=CATEGORY_SYSTEM, code=CATEGORY_CODE, display=CATEGORY_DISPLAY)
                ]
            )
        ],
        subject=Reference(reference=f"Patient/{note.patient_id}"),
        content=[
            DocumentReferenceContent(
                attachment=Attachment(
                    content_type=CONTENT_TYPE,
                    data=base64.b64encode(body.encode("utf-8")).decode("ascii"),
                    title=NOTE_TYPE_DISPLAY,
                )
            )
        ],
        context=DocumentReferenceContext(
            encounter=[Reference(reference=f"Encounter/{note.encounter_id}")]
        ),
    )

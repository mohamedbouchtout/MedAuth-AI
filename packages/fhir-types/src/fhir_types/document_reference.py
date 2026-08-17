"""The FHIR R4 DocumentReference resource.

This is how a generated SOAP note gets back into the EHR — ``write_clinical_note()``
on the adapter base class builds one of these. The note body travels base64-encoded
in ``content[].attachment.data`` and is PHI in full.
"""

from __future__ import annotations

from typing import Literal

from .base import DomainResource, FHIRBase
from .codes import CompositionStatus, DocumentReferenceStatus
from .datatypes import Attachment, CodeableConcept, Coding, Identifier, Period, Reference


class DocumentReferenceContent(FHIRBase):
    """The document itself, plus how it is encoded.

    Attributes:
        attachment: The document content or a URL to it. Required by FHIR.
        format: Format or profile the content conforms to beyond its MIME type.
    """

    attachment: Attachment
    format: Coding | None = None


class DocumentReferenceContext(FHIRBase):
    """The clinical context the document was produced in.

    Attributes:
        encounter: Encounters the document is about — the ambient session's
            Encounter goes here, which is what links a note back to its recording.
        event: Clinical acts the document describes.
        period: Time of service the document covers.
        facility_type: Kind of facility where the patient was seen.
        practice_setting: Clinical specialty of the practice, e.g. orthopedics.
        source_patient_info: The patient demographics as of authoring.
        related: Other resources associated with the document.
    """

    encounter: list[Reference] | None = None
    event: list[CodeableConcept] | None = None
    period: Period | None = None
    facility_type: CodeableConcept | None = None
    practice_setting: CodeableConcept | None = None
    source_patient_info: Reference | None = None
    related: list[Reference] | None = None


class DocumentReference(DomainResource):
    """A reference to a clinical document, with the document attached or linked.

    Attributes:
        master_identifier: Version-specific identifier for the document.
        identifier: Other business identifiers.
        status: Whether the reference is current. Required by FHIR.
        doc_status: Lifecycle state of the underlying document. A SOAP note pending
            physician sign-off is ``preliminary``; ``final`` after attestation.
        type: Kind of document, e.g. a LOINC progress note code.
        category: Broad categorization of the document.
        subject: The patient the document is about.
        date: When the reference was created, as an ``instant``.
        author: Who produced the document.
        authenticator: Who attested to its accuracy.
        custodian: Organization maintaining the document.
        description: Human-readable description. Must not restate note content.
        content: The document itself. Required by FHIR, at least one entry.
        context: Clinical context of the document.
    """

    resource_type: Literal["DocumentReference"] = "DocumentReference"
    master_identifier: Identifier | None = None
    identifier: list[Identifier] | None = None
    status: DocumentReferenceStatus
    doc_status: CompositionStatus | None = None
    type: CodeableConcept | None = None
    category: list[CodeableConcept] | None = None
    subject: Reference | None = None
    date: str | None = None
    author: list[Reference] | None = None
    authenticator: Reference | None = None
    custodian: Reference | None = None
    description: str | None = None
    content: list[DocumentReferenceContent]
    context: DocumentReferenceContext | None = None

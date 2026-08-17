"""The FHIR R4 Condition resource.

Conditions supply the diagnosis codes a payer policy is evaluated against — the
ICD-10 side of the "does this order meet prior authorization criteria" question.
"""

from __future__ import annotations

from typing import Literal

from .base import DomainResource
from .datatypes import Annotation, CodeableConcept, Identifier, Reference


class Condition(DomainResource):
    """A clinical condition, problem or diagnosis.

    ``clinical_status`` and ``verification_status`` are ``CodeableConcept`` rather
    than ``Literal``, matching R4: both have required bindings, but the codes are
    carried inside a coding with a fixed system URI rather than as a bare code.

    ``onset`` and ``abatement`` are FHIR choice elements with five variants each;
    only the ``dateTime`` and ``string`` forms are modelled, because those are what
    the EHRs on our integration list actually send. Anything else survives as an
    extra field rather than being dropped.

    Attributes:
        identifier: Business identifiers for the condition.
        clinical_status: active, recurrence, relapse, inactive, remission, resolved.
        verification_status: unconfirmed through confirmed, refuted, entered-in-error.
        category: problem-list-item or encounter-diagnosis.
        severity: Subjective severity of the condition.
        code: The condition itself, coded — ICD-10 and/or SNOMED.
        body_site: Anatomical location, which several orthopedic policies key on.
        subject: The patient with the condition. Required by FHIR.
        encounter: Encounter during which the condition was first asserted.
        onset_date_time: When the condition began, as a ``dateTime``.
        onset_string: When the condition began, as free text.
        abatement_date_time: When the condition resolved, as a ``dateTime``.
        abatement_string: When the condition resolved, as free text.
        recorded_date: When the record was first captured.
        recorder: Who recorded the condition.
        asserter: Who asserted the condition is present.
        note: Free-text notes about the condition. PHI.
    """

    resource_type: Literal["Condition"] = "Condition"
    identifier: list[Identifier] | None = None
    clinical_status: CodeableConcept | None = None
    verification_status: CodeableConcept | None = None
    category: list[CodeableConcept] | None = None
    severity: CodeableConcept | None = None
    code: CodeableConcept | None = None
    body_site: list[CodeableConcept] | None = None
    subject: Reference
    encounter: Reference | None = None
    onset_date_time: str | None = None
    onset_string: str | None = None
    abatement_date_time: str | None = None
    abatement_string: str | None = None
    recorded_date: str | None = None
    recorder: Reference | None = None
    asserter: Reference | None = None
    note: list[Annotation] | None = None

"""Normalized shapes the adapter layer returns, whatever EHR answered.

These are deliberately not FHIR resources. A ``Coverage`` from Athenahealth and
one from Cerner carry the payer in different places, and the point of the
adapter layer is that nothing above it has to know which. So the primitives that
read a vendor's resources return these, and ``fhir_types``' R4 models stay on
the wire side of the adapter.

Every field here is PHI. Nothing in this module may reach a log line, and a
service reading any of it records the access through hipaa-logger's
``audit_log()``.

The fields are the ones TASK-050 needs in order to type the stubs, drawn from
what TASK-052 and TASK-054 say they return. TASK-052 fills them in and may add
to them; nothing here is populated yet.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from fhir_types import AdministrativeGender, Condition


class PatientInfo(BaseModel):
    """A patient's demographics, flattened out of the FHIR ``Patient`` resource.

    Attributes:
        patient_id: The patient's id on the EHR that answered.
        family_name: Family name, when the resource carried one.
        given_names: Given names, in the order the resource listed them.
        birth_date: Date of birth as a FHIR ``date`` string, when known.
        gender: Administrative gender, used for record matching and for the
            demographics a payer rule may key on (TASK-059). Not a statement
            about gender identity — see ``fhir_types.Patient``.
        address_state: The patient's residence state as a USPS code, when the
            resource carried one this vocabulary recognises. **Not a source for
            the encounter's ``state`` column**, and TASK-052b's only use of it
            is the disagreement warning: the policy documents scope themselves
            by the site of care, so a patient's residence is a different fact
            that merely coincides most of the time. Reading it as the site of
            care is the mistake this field's presence makes easy, which is why
            the ban is written on the field rather than only in
            :mod:`src.adapters.site_of_care`.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: str
    family_name: str | None = None
    given_names: list[str] = []
    birth_date: str | None = None
    gender: AdministrativeGender | None = None
    address_state: str | None = None


class CoverageInfo(BaseModel):
    """The payer half of a patient's context, flattened out of ``Coverage``.

    ``payer`` is the payer's own display name, kept as the resource spelled it.
    It is normalized to a slug by ``/policies/query`` through
    ``payer_vocab.normalize_payer()``, which is the single normalisation site —
    do not slug it here as well. See CLAUDE.md, "Payer and jurisdiction
    identity", and TASK-052b, which is what writes these onto the encounter row.

    Attributes:
        payer: The payer's own display name, as spelled by the resource.
        plan_type: The plan type, from ``Coverage.type`` or ``Coverage.class``.
        member_id: The patient's member id with the payer.
    """

    model_config = ConfigDict(frozen=True)

    payer: str | None = None
    plan_type: str | None = None
    member_id: str | None = None


class PatientContext(BaseModel):
    """Everything a policy query needs about one patient, assembled from three fetches.

    This is what ``EHRAdapter.get_patient_context()`` returns and what
    ``GET /fhir/patient/{patient_id}/context`` answers with.

    Attributes:
        patient: The patient's demographics.
        coverage: The payer half, or None when the EHR returned no usable
            ``Coverage`` at all.
        conditions: The patient's active conditions, as FHIR R4 resources.
        requires_manual_confirmation: True when the payer information is
            incomplete. TASK-052 sets it rather than failing the request: a
            provider filling the payer in is a working encounter, and a guessed
            payer is a cache key standing for a plan the patient is not on.
    """

    model_config = ConfigDict(frozen=True)

    patient: PatientInfo
    coverage: CoverageInfo | None = None
    conditions: list[Condition] = []
    requires_manual_confirmation: bool = False


class EncounterCoverageContext(BaseModel):
    """What one encounter needs written onto its ``encounters`` row.

    TASK-052b. This is what ``EHRAdapter.get_encounter_coverage_context()``
    returns and what ``GET /fhir/encounter/{encounter_id}/coverage-context``
    answers with, and it maps one-to-one onto three columns:
    ``insurance_payer``, ``insurance_plan_type`` and ``state``.

    **It is a separate shape from :class:`PatientContext` because it answers a
    different question.** That one is "what do we know about this patient" and
    is keyed on a patient; this one is "which payer policy set applies to this
    visit, and where did it happen" and is keyed on an encounter. Only this one
    can carry ``state``, because the site of care is a property of the encounter
    and no patient-keyed shape has anywhere honest to put it.

    Attributes:
        encounter_id: The encounter's id on the EHR that answered.
        patient_id: The subject read off ``Encounter.subject``, or None when the
            reference could not be resolved — in which case no coverage was
            read either.
        coverage: The payer half, or None when the EHR held no usable
            ``Coverage``. Reuses :class:`CoverageInfo` rather than flattening
            it, so the enumerated rule in TASK-052 has exactly one shape to
            produce.
        state: The **site-of-care** state as a two-character USPS code, or None
            when neither a ``Location`` nor an ``Organization`` yielded one.
            Never the patient's residence — see
            :mod:`src.adapters.site_of_care` for what the policy documents
            actually say. Normalized through ``payer_vocab.normalize_state`` so
            it speaks the vocabulary ``insurance_policies.state`` is matched
            against.
        requires_manual_confirmation: True when the payer information is
            incomplete, by the same :func:`~src.adapters.base.needs_manual_confirmation`
            rule the patient context uses. **A NULL ``state`` does not set it**,
            deliberately: the flag is about payer details a provider can fill in
            from the patient's card, and nobody at the bedside can supply a site
            of care the EHR did not record. ``resolve_query_parameters()``
            already names ``state`` when it is missing, which is where that gap
            is visible.
    """

    model_config = ConfigDict(frozen=True)

    encounter_id: str
    patient_id: str | None = None
    coverage: CoverageInfo | None = None
    state: str | None = None
    requires_manual_confirmation: bool = False


class PriorAuthSubmission(BaseModel):
    """What came back from submitting a prior authorization, whichever path took it.

    Athenahealth does not support FHIR PAS and submits through CoverMyMeds
    instead (TASK-054), so the two things worth recording are the payer's own
    reference and which path was used.

    Attributes:
        payer_reference_number: The payer's reference for the submission, when
            it returned one.
        submission_method: Which path submitted it. TASK-054 fixes the
            vocabulary when it implements the second path; it is a plain string
            until there is more than one value for it to be wrong about.
    """

    model_config = ConfigDict(frozen=True)

    payer_reference_number: str | None = None
    submission_method: str


class NoteCode(BaseModel):
    """One extracted clinical code, as ``track-a-clinical`` stores and returns it.

    A trimmed mirror of CLAUDE.md's "Extracted clinical codes" contract, holding
    only what a chart write needs. ``source`` is required rather than optional
    because it is the field that decides whether the code may be written at all —
    an entry without one is malformed, not a code of unknown provenance to be
    sent anyway. See :data:`~src.adapters.note_document.SENDABLE_CODE_SOURCES`.

    Attributes:
        code: The code itself, dotted as stored (``M17.11``, not ``M1711``).
        display: The proposing source's own description, when it carried one.
        source: ``llm-extraction``, ``comprehend-medical`` or
            ``provider-accepted``.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    code: str
    display: str | None = None
    source: str


class ClinicalNoteContent(BaseModel):
    """One note and the two identifiers that say which chart entry it belongs on.

    What :meth:`~src.adapters.base.EHRAdapter.write_clinical_note` takes. It is
    one argument rather than five so that a vendor subclass overriding the write
    — Athenahealth is the likely first — changes one signature rather than
    re-listing the parameters, the same reasoning that makes
    ``get_patient_context()`` the override point rather than its primitives.

    **The codes arrive unfiltered.** Filtering is the writer's obligation and it
    happens inside the document builder, so a caller cannot forget it and a
    subclass that reuses the builder inherits it. See CLAUDE.md, "Writing
    clinical data out to the EHR".

    Attributes:
        patient_id: The patient as the EHR knows them — the document's subject.
        encounter_id: The encounter as the EHR knows it. Not a ``session_id``,
            which names the same visit in our namespace and would address
            nothing on a chart.
        subjective: The note's subjective section, when one was generated.
        objective: The objective section.
        assessment: The assessment section.
        plan: The plan section.
        icd10_codes: Every extracted diagnosis, whatever its source, or None
            when the extraction pass never answered.
        reviewed_by_provider: Whether a provider has attested to the note. It
            decides the written document's ``docStatus`` and nothing else; no
            write-back ever sets this flag.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: str
    encounter_id: str
    subjective: str | None = None
    objective: str | None = None
    assessment: str | None = None
    plan: str | None = None
    icd10_codes: list[NoteCode] | None = None
    reviewed_by_provider: bool = False
